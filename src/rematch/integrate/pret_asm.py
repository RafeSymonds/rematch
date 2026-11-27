"""Split a pret-macros assembly TU around functions that moved to C (SPEC §10, Phase 4a).

The TU becomes pieces in link order: runs of assembly functions and runs of C functions. The
linker places every object's `.text` first and then each data section type in object order, so
the TU's data can stay in one assembly piece as long as the C pieces emit no data. That piece
keeps the original file name and every non-text section; other assembly pieces hold only text.

Symbols that now cross a piece boundary are declared: a label defined in one piece and used in
another gets `.global` where it is defined and `.public` where it is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from rematch.inventory.asm_parser import AsmFunction, parse_pret_macros

_SECTION = re.compile(r"^\s*\.(?:text|data|rodata|bss|section\s+\S+.*)\s*$")
_TEXT = re.compile(r"^\s*\.text\s*$")
_LABEL_DEF = re.compile(r"^\s*([A-Za-z_.$][\w.$]*):")  # labels may be indented
_TOKEN = re.compile(r"[A-Za-z_.$][\w.$]*")


class SplitError(Exception):
    pass


@dataclass
class Segment:
    directive: str  # the section directive line
    lines: list[str] = field(default_factory=list)  # body, for non-text segments
    functions: list[AsmFunction] = field(default_factory=list)  # for text segments

    @property
    def is_text(self) -> bool:
        return bool(_TEXT.match(self.directive))


@dataclass
class ParsedTU:
    header: list[str]  # lines before the first section directive
    segments: list[Segment]

    @property
    def functions(self) -> list[AsmFunction]:
        return [f for seg in self.segments if seg.is_text for f in seg.functions]


@dataclass
class Piece:
    kind: str  # "asm" or "c"
    path: str  # repo-relative
    functions: list[str]  # names, address order
    text: str = ""  # assembly pieces only
    holds_data: bool = False


def parse_tu(text: str) -> ParsedTU:
    lines = text.splitlines()
    funcs = parse_pret_macros(text)
    in_func: dict[int, AsmFunction] = {}
    for f in funcs:
        for i in range(f.start_line, f.end_line + 1):
            in_func[i] = f
    header: list[str] = []
    segments: list[Segment] = []
    for i, line in enumerate(lines, 1):
        if i in in_func:
            f = in_func[i]
            if not segments or not segments[-1].is_text:
                raise SplitError(f"{f.name} is outside a .text section")
            if i == f.start_line:
                segments[-1].functions.append(f)
            continue
        if _SECTION.match(line):
            segments.append(Segment(line))
            continue
        if not segments:
            header.append(line)
        elif segments[-1].is_text:
            if line.strip():  # anything but blank lines between functions is not ours to move
                raise SplitError(f"line {i}: {line.strip()!r} in .text outside a function")
        else:
            segments[-1].lines.append(line)
    return ParsedTU(header, segments)


def _addr_name(stem: str, f: AsmFunction) -> str:
    if f.address is None:
        raise SplitError(f"{f.name} has no `NAME: ; 0xADDR` label")
    return f"{stem}_{f.address:08X}"


def plan_pieces(tu_path: str, parsed: ParsedTU, in_c: set[str]) -> list[Piece]:
    """Pieces in link order for the functions in `in_c` moving to C."""
    path = PurePosixPath(tu_path)
    stem = path.stem
    funcs = parsed.functions
    runs: list[tuple[str, list[AsmFunction]]] = []
    for f in funcs:
        kind = "c" if f.name in in_c else "asm"
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(f)
        else:
            runs.append((kind, [f]))
    has_data = any(not seg.is_text for seg in parsed.segments)
    pieces: list[Piece] = []
    data_placed = False
    for kind, run in runs:
        names = [f.name for f in run]
        if kind == "c":
            name = stem if run[0] is funcs[0] else _addr_name(stem, run[0])
            pieces.append(Piece("c", f"src/{name}.c", names))
        elif not data_placed:
            pieces.append(Piece("asm", tu_path, names, holds_data=True))
            data_placed = True
        else:
            pieces.append(Piece("asm", str(path.with_name(_addr_name(stem, run[0]) + ".s")), names))
    if has_data and not data_placed:
        pieces.insert(0, Piece("asm", tu_path, [], holds_data=True))
    return pieces


def _body(parsed: ParsedTU, piece: Piece) -> list[str]:
    own = set(piece.functions)
    out: list[str] = []
    if piece.holds_data:
        for seg in parsed.segments:
            if seg.is_text:
                kept = [f for f in seg.functions if f.name in own]
                if not kept:
                    continue
                out += [seg.directive, ""]
                for f in kept:
                    out += [*f.text.splitlines(), ""]
            else:
                out += [seg.directive, *seg.lines]
        return out
    out += ["\t.text", ""]
    for f in parsed.functions:
        if f.name in own:
            out += [*f.text.splitlines(), ""]
    return out


def defined_labels(lines: list[str]) -> set[str]:
    return {m.group(1) for line in lines if (m := _LABEL_DEF.match(line))}


def used_tokens(lines: list[str]) -> set[str]:
    out: set[str] = set()
    for line in lines:
        code = line.split(";", 1)[0]
        out.update(_TOKEN.findall(code))
    return out


def render_asm_pieces(
    parsed: ParsedTU, pieces: list[Piece], c_undefined: set[str]
) -> dict[str, str]:
    """Text of every assembly piece. `c_undefined` is every symbol the C pieces reference."""
    bodies = {p.path: _body(parsed, p) for p in pieces if p.kind == "asm"}
    defs = {path: defined_labels(body) for path, body in bodies.items()}
    c_defs = {name for p in pieces if p.kind == "c" for name in p.functions}
    out: dict[str, str] = {}
    for path, body in bodies.items():
        used = used_tokens(body)
        elsewhere = set().union(*(d for q, d in defs.items() if q != path)) | c_defs
        imports = sorted((used & elsewhere) - defs[path])
        wanted = c_undefined.union(*(used_tokens(b) for q, b in bodies.items() if q != path))
        exports = sorted(defs[path] & wanted)
        decls = [f"\t.public {n}" for n in imports] + [f"\t.global {n}" for n in exports]
        text = [*parsed.header]
        if decls:
            text += ["", "\t; rematch: symbols shared with the other pieces of this TU", *decls]
        text += ["", *body]
        out[path] = "\n".join(text).rstrip("\n") + "\n"
    return out
