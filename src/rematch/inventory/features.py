"""Per-function features from normalized disassembly, and difficulty (SPEC §6.1, §8.3)."""

from __future__ import annotations

from dataclasses import dataclass

from rematch.diff.objdump_backend import Line, is_branch, is_call


@dataclass(frozen=True)
class Features:
    n_insns: int
    n_branches: int
    has_switch: bool
    pool_entries: int
    callees: tuple[str, ...]


def _writes_pc(line: Line) -> bool:
    """Computed jumps: `add pc, rX`, `mov pc, rX`, `ldr pc, [...]` (jump tables), not returns."""
    base = line.mnemonic.split(".")[0]
    dest = line.operands.split(",")[0].strip()
    return dest == "pc" and base.rstrip("s")[:3] in ("add", "mov", "ldr")


def features(lines: list[Line]) -> Features:
    callees: list[str] = []
    for line in lines:
        if is_call(line) and line.reloc and line.reloc not in callees:
            callees.append(line.reloc)
    return Features(
        n_insns=sum(1 for line in lines if not line.is_data),
        n_branches=sum(1 for line in lines if not line.is_data and is_branch(line.mnemonic)),
        has_switch=any(_writes_pc(line) for line in lines if not line.is_data),
        pool_entries=sum(1 for line in lines if line.mnemonic == ".word"),
        callees=tuple(callees),
    )


def difficulty(f: Features, unknown_callees: int, max_sim: float = 0.0) -> float:
    """SPEC §8.3. `max_sim` stays 0 until similarity exists (Phase 3)."""
    return (
        f.n_insns
        + 8 * f.n_branches
        + 25 * int(f.has_switch)
        + 2 * f.pool_entries
        + 10 * unknown_callees
        - 40 * max_sim
    )
