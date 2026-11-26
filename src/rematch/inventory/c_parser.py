"""Top-level C function definitions: find bodies, extract sources, build TU-local context.

A light scanner, not a C parser: it tracks comments, string/char literals, preprocessor lines
and brace depth, which is enough to locate top-level function bodies in decomp-style C.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_IDENT_BEFORE_PAREN = re.compile(r"([A-Za-z_]\w*)\s*$")
_PTR_RETURNING = re.compile(r"\*\s*([A-Za-z_]\w*)\s*\([^()]*\)\s*\)\s*$")
_INCLUDE = re.compile(r"^\s*#\s*include\b.*$", re.MULTILINE)


@dataclass(frozen=True)
class CFunction:
    name: str
    decl_start: int  # offset where the definition's declaration starts
    body_start: int  # offset of the opening brace
    body_end: int  # offset just past the closing brace

    def header(self, text: str) -> str:
        return text[self.decl_start : self.body_start].strip()

    def source(self, text: str) -> str:
        return text[self.decl_start : self.body_end]

    def is_inline(self, text: str) -> bool:
        return re.search(r"\binline\b", self.header(text)) is not None


def mask(text: str) -> str:
    """Same length as `text`, with comments, literals and preprocessor lines blanked out."""
    out = list(text)
    i, n = 0, len(text)
    line_start = True
    while i < n:
        c = text[i]
        if line_start and c in " \t":
            i += 1
            continue
        if line_start and c == "#":
            # Preprocessor line, with backslash continuations.
            j = i
            while j < n and text[j] != "\n":
                if text[j] == "\\" and j + 1 < n and text[j + 1] == "\n":
                    j += 2
                    continue
                j += 1
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        line_start = c == "\n"
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            i = j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        if c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out[i + 1 : j - 1] = " " * max(0, j - 1 - (i + 1))
            i = j
            continue
        i += 1
    return "".join(out)


def parse_c_functions(text: str) -> list[CFunction]:
    masked = mask(text)
    funcs: list[CFunction] = []
    depth = 0
    decl_start = 0
    i, n = 0, len(masked)
    while i < n:
        c = masked[i]
        if c == "{":
            if depth == 0:
                before = masked[decl_start:i].rstrip()
                if before.endswith(")"):
                    name = _function_name(before)
                    end = _matching_brace(masked, i)
                    if name is not None and end is not None:
                        start = decl_start
                        while start < i and masked[start].isspace():
                            start += 1
                        funcs.append(CFunction(name, start, i, end))
                        i = end
                        decl_start = end
                        continue
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                decl_start = i + 1
        elif depth == 0 and (
            c == ";" or (c == "\n" and text[decl_start:i].lstrip().startswith("#"))
        ):
            decl_start = i + 1
        i += 1
    return funcs


def _function_name(before: str) -> str | None:
    """Name of the function whose parameter list closes at the end of `before`."""
    depth = 0
    for j in range(len(before) - 1, -1, -1):
        ch = before[j]
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
            if depth == 0:
                m = _IDENT_BEFORE_PAREN.search(before[:j])
                if m is None:
                    # A function returning a function pointer: `T (*Name(params))(args)`.
                    ptr = _PTR_RETURNING.search(before[:j])
                    return ptr.group(1) if ptr else None
                if m.group(1) in ("if", "while", "for", "switch", "return", "sizeof"):
                    return None
                return m.group(1)
    return None


def _matching_brace(masked: str, open_pos: int) -> int | None:
    depth = 0
    for j in range(open_pos, len(masked)):
        if masked[j] == "{":
            depth += 1
        elif masked[j] == "}":
            depth -= 1
            if depth == 0:
                return j + 1
    return None


def function_source(text: str, name: str) -> str | None:
    for func in parse_c_functions(text):
        if func.name == name:
            return func.source(text)
    return None


def tu_local_context(text: str, target: str) -> str:
    """The TU's own declarations for compiling `target` in isolation.

    `#include` lines are removed (the header context provides them), the target's definition
    is removed, inline function bodies are kept, and every other body becomes a prototype.
    """
    funcs = parse_c_functions(text)
    pieces: list[str] = []
    pos = 0
    for func in funcs:
        pieces.append(_unstatic_prototypes(text[pos : func.decl_start], target))
        if func.name == target:
            pass
        elif func.is_inline(text):
            pieces.append(func.source(text))
        else:
            pieces.append(_STATIC.sub("", func.header(text), count=1) + ";")
        pos = func.body_end
    pieces.append(_unstatic_prototypes(text[pos:], target))
    return _INCLUDE.sub("", "".join(pieces))


# A `static` (non-inline) function prototype: `static T Name(params);`.
_STATIC_PROTO = re.compile(
    r"\bstatic\s+(?!inline\b)(?P<rest>[^;{}=]*?\b(?P<name>[A-Za-z_]\w*)\s*\((?!\s*\*)[^;{}]*\)\s*;)"
)


def _unstatic_prototypes(chunk: str, target: str) -> str:
    """Other functions' static prototypes become plain ones.

    Their bodies are not compiled in isolation, and mwcc rejects a call to a static function it
    never sees defined. The target's own declarations keep `static` so the candidate may use it.
    """

    def repl(m: re.Match[str]) -> str:
        return m.group(0) if m.group("name") == target else m.group("rest")

    return _STATIC_PROTO.sub(repl, chunk)


_STATIC = re.compile(r"\bstatic\b\s*")


def drop_static(text: str, name: str) -> str:
    """Remove `static` from `name`'s definition so an isolated compile still emits it.

    An unreferenced static function is not emitted at all, and in isolation nothing references
    it. Linkage does not change the function's code.
    """
    for func in parse_c_functions(text):
        if func.name == name:
            header = text[func.decl_start : func.body_start]
            return (
                text[: func.decl_start] + _STATIC.sub("", header, count=1) + text[func.body_start :]
            )
    return text
