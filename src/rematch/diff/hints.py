"""Deterministic hint rules over an alignment (SPEC §6.6). One rule set per ISA mode.

Hints are suggestions. Each rule has a fixture test; add a rule only with one.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable

from rematch.diff.objdump_backend import Line, is_call
from rematch.diff.score import Op

Rule = Callable[[list[Op]], str | None]

_UNSIGNED = {"hs", "lo", "hi", "ls", "cs", "cc"}
_SIGNED = {"ge", "lt", "gt", "le"}
_REGISTER = re.compile(r"\b(?:r\d{1,2}|ip|lr|sp|fp|sl)\b")


def _base(mnemonic: str) -> str:
    return mnemonic.split(".")[0]


def _cond(mnemonic: str) -> str | None:
    base = _base(mnemonic)
    return base[1:] if base.startswith("b") and len(base) == 3 else None


def _shift_amount(line: Line) -> str | None:
    m = re.search(r"#(?:0x)?(\w+)$", line.operands)
    return m.group(1) if m else None


def width_mismatch(ops: list[Op]) -> str | None:
    """Extra or missing lsl #24/#16 followed by lsr/asr: a narrower or wider type."""
    for side in ("extra", "missing"):
        lines = [op.candidate if side == "extra" else op.target for op in ops if op.kind == side]
        for a, b in itertools.pairwise(lines):
            if a is None or b is None:
                continue
            amount = _shift_amount(a)
            if (
                _base(a.mnemonic).startswith("lsl")
                and amount in ("24", "18", "16", "10")
                and _base(b.mnemonic)[:3] in ("lsr", "asr")
                and _shift_amount(b) == amount
            ):
                signed = "signed" if _base(b.mnemonic).startswith("asr") else "unsigned"
                where = "your code" if side == "extra" else "the original"
                return (
                    f"extra lsl/{_base(b.mnemonic)[:3]} pair in {where} → {signed} narrow type "
                    "(u8/s8/u16/s16 vs int) differs"
                )
    return None


def signedness(ops: list[Op]) -> str | None:
    """bhs/blo/bhi/bls vs bge/blt/bgt/ble: signed vs unsigned comparison."""
    for op in ops:
        if op.kind != "opcode" or op.target is None or op.candidate is None:
            continue
        t, c = _cond(op.target.mnemonic), _cond(op.candidate.mnemonic)
        if t and c and (t in _UNSIGNED) != (c in _UNSIGNED) and (t in _SIGNED or c in _SIGNED):
            return (
                f"{_base(op.target.mnemonic)} vs {_base(op.candidate.mnemonic)} → "
                "signedness of a comparison differs"
            )
    return None


def register_permutation(ops: list[Op]) -> str | None:
    """Same instructions, permuted registers: declaration order, temporaries, operand order."""
    diffs = [op for op in ops if op.kind != "equal"]
    if not diffs or any(op.kind != "operands" for op in diffs):
        return None
    for op in diffs:
        assert op.target is not None and op.candidate is not None
        if _REGISTER.sub("R", op.target.operands) != _REGISTER.sub("R", op.candidate.operands):
            return None
    return (
        "same instructions with registers permuted → try declaration order, temporaries, or "
        "expression operand order"
    )


def push_pop_sets(ops: list[Op]) -> str | None:
    """push/pop register lists differ: a different number of simultaneously live variables."""
    for op in ops:
        if op.kind == "operands" and op.target and _base(op.target.mnemonic) in ("push", "pop"):
            theirs = op.candidate.operands if op.candidate else ""
            return (
                f"{_base(op.target.mnemonic)} {op.target.operands} vs {theirs}"
                " → number of simultaneously live variables differs"
            )
    return None


def _writes_pc(line: Line | None) -> bool:
    return line is not None and line.operands.split(",")[0].strip() == "pc" and not line.is_data


def jump_table(ops: list[Op]) -> str | None:
    """A jump table on one side only: switch vs if chain, or case density."""
    t = any(_writes_pc(op.target) for op in ops)
    c = any(_writes_pc(op.candidate) for op in ops)
    if t and not c:
        return (
            "the original uses a jump table and yours doesn't → switch vs if chain, or case density"
        )
    if c and not t:
        return (
            "yours uses a jump table and the original doesn't → if chain vs switch, or case density"
        )
    return None


def inline_vs_call(ops: list[Op]) -> str | None:
    """A call on one side replaced by inline code on the other."""
    t_calls = [op.target.reloc for op in ops if op.target and is_call(op.target)]
    c_calls = [op.candidate.reloc for op in ops if op.candidate and is_call(op.candidate)]
    only_t = sorted(set(filter(None, t_calls)) - set(filter(None, c_calls)))
    only_c = sorted(set(filter(None, c_calls)) - set(filter(None, t_calls)))
    # Inline code shows up as candidate-only or replaced lines (and the reverse for missing).
    extra = sum(1 for op in ops if op.kind in ("extra", "opcode"))
    missing = sum(1 for op in ops if op.kind in ("missing", "opcode"))
    if only_t and extra >= 2:
        return (
            f"the original calls {only_t[0]} where yours has inline code → "
            "call it (inline function or macro vs call)"
        )
    if only_c and missing >= 2:
        return (
            f"yours calls {only_c[0]} where the original has inline code → inline function or macro"
        )
    return None


def pool_value(ops: list[Op]) -> str | None:
    """A literal pool value differs: wrong constant, enum or symbol."""
    for op in ops:
        if op.kind != "operands" or op.target is None or op.candidate is None:
            continue
        both_words = op.target.mnemonic == ".word" == op.candidate.mnemonic
        both_loads = "=" in op.target.operands and "=" in op.candidate.operands
        if both_words or both_loads:
            t = op.target.operands.split("=")[-1]
            c = op.candidate.operands.split("=")[-1]
            return f"literal {t} vs {c} → wrong constant, enum or symbol"
    return None


ARM_THUMB_RULES: tuple[Rule, ...] = (
    width_mismatch,
    signedness,
    register_permutation,
    push_pop_sets,
    jump_table,
    inline_vs_call,
    pool_value,
)

RULE_SETS: dict[str, tuple[Rule, ...]] = {"thumb": ARM_THUMB_RULES, "arm": ARM_THUMB_RULES}


def hints(ops: list[Op], mode: str) -> list[tuple[str, str]]:
    """(rule id, text) for every rule that fires. The rule id is the function name."""
    out: list[tuple[str, str]] = []
    for rule in RULE_SETS.get(mode, ()):
        hint = rule(ops)
        if hint is not None:
            out.append((rule.__name__, hint))
    return out
