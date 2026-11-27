"""Compose a C piece from matched candidates (SPEC §10, Phase 4a).

Each candidate compiled against its sandbox ctx.h, which is the flattened content of the TU
template's headers. The piece includes those headers instead, then every candidate in address
order with `#include "ctx.h"` removed and repeated top-level declarations kept once.
"""

from __future__ import annotations

import re

_CTX_INCLUDE = re.compile(r'^\s*#\s*include\s*"ctx\.h"\s*$')
_TYPES_BLOCK = re.compile(r"/\*\s*rematch:types\s*\*/.*?/\*\s*rematch:end-types\s*\*/", re.S)


def _top_level_lines(source: str) -> list[tuple[str, bool]]:
    """(line, is_top_level_declaration) for every line, tracking brace depth."""
    out: list[tuple[str, bool]] = []
    depth = 0
    in_comment = False
    for line in source.splitlines():
        stripped = line.strip()
        decl = (
            depth == 0
            and not in_comment
            and stripped.endswith(";")
            and "{" not in stripped
            and not stripped.startswith(("typedef struct", "typedef union"))
        )
        out.append((line, decl))
        code = re.sub(r'"(?:\\.|[^"\\])*"', "", line)
        if "/*" in code and "*/" not in code.split("/*", 1)[1]:
            in_comment = True
        if in_comment and "*/" in code:
            in_comment = False
        code = re.sub(r"/\*.*?\*/|//.*", "", code)
        depth += code.count("{") - code.count("}")
    return out


def definition_header(name: str, source: str) -> str | None:
    """`T name(args)` of the definition of `name` in `source`, as one line."""
    m = re.search(
        rf"^([^\n;{{}}#]*\b{re.escape(name)}\s*\([^;{{}}]*\))\s*\{{", source, re.MULTILINE
    )
    return " ".join(m.group(1).split()) if m else None


def compose_piece(includes: list[str], sources: list[tuple[str, str]]) -> str:
    """`includes`: header names. `sources`: (function name, candidate) in address order.

    Prototypes of functions defined in the piece are replaced by one prototype per function,
    taken from its definition, so candidates that guessed a sibling's signature agree.
    """
    seen: set[str] = set()
    own = [name for name, _ in sources]
    own_call = re.compile(r"\b(" + "|".join(map(re.escape, own)) + r")\s*\(") if own else None
    parts = [f'#include "{h}"' for h in dict.fromkeys(includes)]
    # provisional types first (names are address-unique), then prototypes that may use them
    stripped: list[tuple[str, str]] = []
    for name, source in sources:
        m = _TYPES_BLOCK.search(source)
        if m:
            inner = re.sub(r"/\*\s*rematch:(?:end-)?types\s*\*/", "", m.group(0))
            parts += ["", inner.strip()]
            source = source[: m.start()] + source[m.end() :]
        stripped.append((name, source))
    protos = [h for name, src in stripped if (h := definition_header(name, src))]
    if protos:
        parts += ["", *(f"{h};" for h in protos)]
    for _name, source in stripped:
        body: list[str] = []
        for line, decl in _top_level_lines(source):
            if _CTX_INCLUDE.match(line):
                continue
            if decl and own_call is not None and own_call.search(line) and "=" not in line:
                continue  # a prototype of a function defined in this piece
            key = " ".join(line.split())
            if decl:
                if key in seen:
                    continue
                seen.add(key)
            body.append(line)
        parts += ["", re.sub(r"\n{3,}", "\n\n", "\n".join(body)).strip("\n")]
    return "\n".join(parts).rstrip("\n") + "\n"
