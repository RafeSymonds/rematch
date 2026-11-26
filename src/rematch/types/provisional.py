"""Provisional types: the `/* rematch:types */` block in candidate.c (SPEC §16.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rematch.inventory.c_parser import mask, strip_comments

BEGIN = "/* rematch:types */"
END = "/* rematch:end-types */"
_BLOCK = re.compile(r"/\*\s*rematch:types\s*\*/(?P<body>.*?)/\*\s*rematch:end-types\s*\*/", re.S)
_BEGIN_ANY = re.compile(r"/\*\s*rematch:types\s*\*/")
TYPE_NAME = re.compile(r"^Unk(?:Struct|Union)_[0-9A-F]{8}(?:_\d+)?$")
# unk_XX by hex offset, optionally with a suffix for bitfields or split fields (unk_58_0).
FIELD_NAME = re.compile(r"^unk_[0-9A-Fa-f]+(?:_[0-9A-Za-z]+)?$")
_TYPEDEF = re.compile(
    r"^typedef\s+(?P<kind>struct|union)\s*(?P<tag>\w+)?\s*\{(?P<body>.*)\}\s*(?P<name>\w+)$", re.S
)
_FIELD = re.compile(
    r"^(?P<type>.+?)\b(?P<name>\w+)\s*(?P<array>(?:\[[^\]]*\])*)\s*(?::\s*\d+)?$", re.S
)


class TypesError(Exception):
    pass


@dataclass(frozen=True)
class ProvField:
    name: str
    decl: str  # "u16 unk_2C" as written
    bitfield: bool


@dataclass(frozen=True)
class ProvType:
    name: str
    kind: str  # struct | union
    fields: tuple[ProvField, ...]
    source: str  # the typedef as written


def _split_top(text: str) -> list[str]:
    """Split on `;` at brace depth 0."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, c in enumerate(text):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ";" and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def extract_block(candidate: str) -> str | None:
    """The block's body, or None. Raises TypesError if it is misplaced or repeated."""
    begins = len(_BEGIN_ANY.findall(candidate))
    if begins == 0:
        return None
    if begins > 1:
        raise TypesError("more than one rematch:types block")
    m = _BLOCK.search(candidate)
    if m is None:
        raise TypesError("rematch:types block has no rematch:end-types")
    before = strip_comments(candidate[: m.start()]).strip()
    if before:
        raise TypesError("the rematch:types block must come first in candidate.c")
    return m.group("body")


def parse_block(body: str) -> list[ProvType]:
    """Typedef'd structs and unions only, with `UnkStruct_<ADDR>` names and `unk_XX` fields."""
    types: list[ProvType] = []
    clean = strip_comments(body)
    for stmt in _split_top(clean):
        m = _TYPEDEF.match(" ".join(stmt.split()))
        if m is None:
            raise TypesError(f"only `typedef struct|union {{...}} Name;` is allowed: {stmt[:60]!r}")
        name = m.group("name")
        if not TYPE_NAME.match(name):
            raise TypesError(
                f"type name {name!r} must look like UnkStruct_<ADDR> or UnkUnion_<ADDR>"
            )
        fields: list[ProvField] = []
        for decl in _split_top(m.group("body")):
            if "{" in decl:
                raise TypesError(f"{name}: nested struct/union members are not allowed")
            f = _FIELD.match(decl)
            if f is None:
                raise TypesError(f"{name}: cannot read field {decl!r}")
            if not FIELD_NAME.match(f.group("name")):
                raise TypesError(f"{name}: field {f.group('name')!r} must be named unk_XX")
            fields.append(ProvField(f.group("name"), decl, ":" in decl))
        if not fields:
            raise TypesError(f"{name} has no fields")
        types.append(ProvType(name, m.group("kind"), tuple(fields), stmt + ";"))
    return types


def conflicts(types: list[ProvType], ctx: str) -> list[str]:
    """Names the block declares that the context already declares."""
    masked = mask(ctx)
    return [t.name for t in types if re.search(rf"\b{re.escape(t.name)}\b", masked)]


def check_block(candidate: str, ctx: str) -> str | None:
    """Pre-filter reason (SPEC §6.4 step 1), or None."""
    try:
        body = extract_block(candidate)
        if body is None:
            return None
        dupes = conflicts(parse_block(body), ctx)
    except TypesError as exc:
        return str(exc)
    if dupes:
        return f"rematch:types redefines names ctx.h declares: {', '.join(dupes)}"
    return None
