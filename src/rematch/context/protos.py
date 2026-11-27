"""Prototype overrides (SPEC §6.4, §10).

A target's headers often carry placeholder prototypes for functions still in assembly
(`void f(void);` for a function that takes three arguments and returns a value). A worker
can't define such a function, because its definition conflicts with the header. A candidate
may therefore redeclare the target, or an assembly-only callee, in one block after the
provisional types:

    /* rematch:protos */
    BOOL f(u16 a, u8 b);
    /* rematch:end-protos */

./check removes those names' declarations from ctx.h before compiling. Integration replaces
the declarations in the target's headers (target.toml [worker] header_dirs), and the batch
build checks every other caller.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

_BLOCK = re.compile(
    r"/\*\s*rematch:protos\s*\*/(?P<body>.*?)/\*\s*rematch:end-protos\s*\*/[ \t]*\n?", re.S
)
_BEGIN = re.compile(r"/\*\s*rematch:protos\s*\*/")
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_PARAMS = r"[^;{}()]*(?:\([^;{}()]*\)[^;{}()]*)*"
_PROTO = re.compile(
    rf"^(?!typedef\b)[A-Za-z_][\w\s\*]*?(?<![\w$])(?P<name>[A-Za-z_]\w*)\s*\((?:{_PARAMS})\)$"
)


class ProtosError(ValueError):
    pass


def extract(candidate: str) -> dict[str, str]:
    """name -> prototype (`T name(params);`, whitespace normalized) from the protos block."""
    blocks = list(_BLOCK.finditer(candidate))
    if not blocks:
        if _BEGIN.search(candidate):
            raise ProtosError("the rematch:protos block has no /* rematch:end-protos */")
        return {}
    if len(blocks) > 1:
        raise ProtosError("candidate.c has more than one rematch:protos block")
    out: dict[str, str] = {}
    for stmt in _COMMENT.sub(" ", blocks[0].group("body")).split(";"):
        text = " ".join(stmt.split())
        if not text:
            continue
        m = _PROTO.match(text)
        if m is None:
            raise ProtosError(f"rematch:protos holds only function prototypes, not: {text[:80]}")
        if m.group("name") in out:
            raise ProtosError(f"rematch:protos declares {m.group('name')} twice")
        out[m.group("name")] = text + ";"
    return out


def remove_block(source: str) -> str:
    return _BLOCK.sub("", source)


def _statements(text: str) -> Iterator[tuple[int, int]]:
    """(start, end) of each top-level statement ending in ';', skipping preprocessor lines,
    comments and literals. `start` is the first character of code."""
    i, n, depth = 0, len(text), 0
    start: int | None = None
    line_start = True
    while i < n:
        c = text[i]
        if line_start and c == "#":
            while True:  # to the end of the directive, following continuations
                j = text.find("\n", i)
                if j < 0:
                    i = n
                    break
                i = j + 1
                if j == 0 or text[j - 1] != "\\":
                    break
            line_start = True
            continue
        if c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            if depth == 0 and start is None:
                start = i
            i = j + 1
            line_start = False
            continue
        if c == "\n":
            line_start = True
            i += 1
            continue
        if not c.isspace():
            line_start = False
            if depth == 0 and start is None and c not in "{};":
                start = i
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
            if depth == 0:
                start = None  # a definition or a struct body: what follows is a new statement
        elif c == ";" and depth == 0:
            if start is not None:
                yield start, i + 1
            start = None
        i += 1


def _declares(stmt: str, name: str) -> bool:
    """True if `stmt` declares function `name` (its declarator, not a parameter)."""
    if re.match(r"\s*typedef\b", stmt):
        return False
    for m in re.finditer(rf"(?<![\w$]){re.escape(name)}\s*\(", stmt):
        before = stmt[: m.start()]
        if before.count("(") == before.count(")"):
            return True
    return False


def declaration_spans(text: str, name: str) -> list[tuple[int, int]]:
    return [(a, b) for a, b in _statements(text) if _declares(text[a:b], name)]


def strip_declarations(text: str, names: list[str]) -> tuple[str, set[str]]:
    """`text` without the top-level declarations of `names`; also the names it declared."""
    spans: list[tuple[int, int]] = []
    found: set[str] = set()
    quick = re.compile(r"(?<![\w$])(" + "|".join(map(re.escape, names)) + r")\s*\(")
    for a, b in _statements(text):
        stmt = text[a:b]
        hit = [n for n in {m.group(1) for m in quick.finditer(stmt)} if _declares(stmt, n)]
        if hit:
            found.update(hit)
            spans.append((a, b))
    for a, b in reversed(spans):
        text = text[:a] + text[b:]
    return text, found


def replace_declarations(text: str, name: str, decl: str) -> str | None:
    """`text` with every declaration of `name` replaced by `decl`; None if it has none."""
    spans = declaration_spans(text, name)
    if not spans:
        return None
    for a, b in reversed(spans):
        text = text[:a] + decl + text[b:]
    return text


_C_TYPE_WORDS = frozenset(
    [
        "void",
        "char",
        "short",
        "int",
        "long",
        "float",
        "double",
        "signed",
        "unsigned",
        "const",
        "volatile",
        "struct",
        "union",
        "enum",
    ]
)


def _split_top(text: str) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    for ch in text:
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        depth += ch in "([{"
        depth -= ch in ")]}"
        cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def parameter_types(decl: str) -> list[str] | None:
    """The parameter types of a prototype, names dropped (`(void)` is no parameters); None if
    `decl` is not a prototype."""
    m = re.search(rf"\((?P<params>{_PARAMS})\)\s*;?\s*$", " ".join(decl.split()))
    if m is None:
        return None
    out = []
    for param in _split_top(m.group("params")):
        param = re.sub(r"\(\s*\*\s*[A-Za-z_]\w*\s*\)", "(*)", param)  # function pointer name
        named = re.match(
            r"^(?P<type>.*[\w\s\*])\b(?P<name>[A-Za-z_]\w*)\s*(?P<dims>(\[[^\]]*\])*)$", param
        )
        if named and named["name"] not in _C_TYPE_WORDS and re.search(r"\w", named["type"]):
            param = named["type"] + named["dims"]
        out.append(" ".join(param.replace("*", " * ").split()))
    return [] if out == ["void"] else out


def declared_prototype(ctx: str, name: str) -> str | None:
    spans = declaration_spans(ctx, name)
    return " ".join(ctx[spans[0][0] : spans[0][1]].split()) if spans else None
