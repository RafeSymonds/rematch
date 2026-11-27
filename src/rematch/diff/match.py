"""Binary match of one function symbol between two objects (SPEC §6.5).

Match means: same size, same code bytes once relocation sites are zeroed, and the same
relocation list (offset, kind, normalized target). Target normalization:
- named symbols compare by name and addend; the function's own name compares as `<self>`
- anonymous or section-relative targets (string literals, `@N`) compare by referenced bytes
- calls compare by callee and call kind (`bl`/`blx`); a call the assembler resolved without a
  relocation (callee in the same section) counts as a call to that callee
Everything else is byte-exact.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rematch.diff.objdump_backend import Line, describe_target
from rematch.toolchain.objects import ObjectFile, Symbol

Resolver = Callable[[Symbol, int], str | None]  # (target symbol, addend) -> name or None

_CALL_TYPES = {
    "R_ARM_THM_CALL": 4,
    "R_ARM_THM_PC22": 4,
    "R_ARM_THM_XPC22": 4,
    "R_ARM_CALL": 8,
    "R_ARM_PC24": 8,
    "R_ARM_XPC25": 8,
    "R_ARM_JUMP24": 8,
    "R_ARM_THM_JUMP24": 4,
}


@dataclass(frozen=True)
class RelocEntry:
    offset: int  # relative to the function start
    kind: str  # 'call:bl', 'call:blx' or the relocation type
    target: str  # normalized target


@dataclass(frozen=True)
class FunctionView:
    name: str
    size: int
    code: bytes  # relocation sites zeroed
    relocs: tuple[RelocEntry, ...]


@dataclass(frozen=True)
class MatchResult:
    match: bool
    reasons: tuple[str, ...]


def function_view(
    obj: ObjectFile,
    name: str,
    lines: list[Line],
    self_name: str,
    resolve: Resolver | None = None,
) -> FunctionView:
    """`resolve`, when given, names non-call targets first (e.g. by location, SPEC §10)."""
    sym = obj.function(name)
    if sym is None or sym.section is None:
        raise KeyError(f"{name} is not a sized function in {obj.path}")
    section = obj.sections[sym.section]
    code = bytearray(section.data[sym.value : sym.value + sym.size])
    mnemonic_at = {line.offset: line.mnemonic.split(".")[0] for line in lines}
    # pool words keep the name the lines show (it may come from the assembly source)
    word_target = {ln.offset: ln.reloc for ln in lines if ln.mnemonic == ".word" and ln.reloc}
    entries: dict[int, RelocEntry] = {}
    for r in section.relocs:
        if not sym.value <= r.offset < sym.value + sym.size:
            continue
        off = r.offset - sym.value
        if r.type in _CALL_TYPES:
            target = describe_target(obj, r.symbol, r.addend + _CALL_TYPES[r.type], self_name)
            kind = f"call:{mnemonic_at.get(off, 'bl')}"
        else:
            located = resolve(r.symbol, r.addend) if resolve is not None else None
            target = (
                located
                or word_target.get(off)
                or describe_target(obj, r.symbol, r.addend, self_name)
            )
            kind = r.type
        entries[off] = RelocEntry(off, kind, target)
        code[off : off + r.width] = bytes(r.width)
    for line in lines:
        base = line.mnemonic.split(".")[0]
        if base in ("bl", "blx") and line.reloc is not None and line.offset not in entries:
            entries[line.offset] = RelocEntry(line.offset, f"call:{base}", line.reloc)
            code[line.offset : line.offset + 4] = bytes(4)
    ordered = tuple(entries[k] for k in sorted(entries))
    return FunctionView(name, sym.size, bytes(code), ordered)


def binary_match(target: FunctionView, candidate: FunctionView) -> MatchResult:
    reasons: list[str] = []
    if target.size != candidate.size:
        reasons.append(f"size {candidate.size:#x}, target {target.size:#x}")
    if target.code != candidate.code:
        first = next(
            (
                i
                for i, (a, b) in enumerate(zip(target.code, candidate.code, strict=False))
                if a != b
            ),
            min(len(target.code), len(candidate.code)),
        )
        reasons.append(f"code bytes differ from offset {first:#x}")
    if target.relocs != candidate.relocs:
        t = {(e.offset, e.kind, e.target) for e in target.relocs}
        c = {(e.offset, e.kind, e.target) for e in candidate.relocs}
        missing = sorted(t - c)[:3]
        extra = sorted(c - t)[:3]
        reasons.append(f"relocations differ (missing {missing}, extra {extra})")
    return MatchResult(not reasons, tuple(reasons))
