"""Text the worker sees after `./check` (SPEC §6.6)."""

from __future__ import annotations

from dataclasses import dataclass

from rematch.diff.score import Op

MAX_LINES = 60
CONTEXT = 1
_LABEL = {"operands": "~operands", "opcode": "~opcode", "extra": "+extra", "missing": "-missing"}
_COL = 26


@dataclass(frozen=True)
class VersionResult:
    version: str
    match: bool
    score: float
    n_target: int
    n_candidate: int
    ops: list[Op]
    hints: list[tuple[str, str]]  # (rule id, text)
    reasons: tuple[str, ...] = ()  # why a 100-score result is not a binary match


def display_score(result: VersionResult) -> float:
    """100 without a binary match shows as 99.9 (SPEC §6.5)."""
    if result.score >= 100.0 and not result.match:
        return 99.9
    return round(result.score, 1)


def header(check: int, max_checks: int, current: float, best: float, r: VersionResult) -> str:
    return (
        f"check {check}/{max_checks} | score {current:.1f} (best {best:.1f}) | "
        f"target {r.n_target} insns, yours {r.n_candidate} | version {r.version}"
    )


def render_match(check: int, max_checks: int) -> str:
    return f"MATCH (check {check}/{max_checks})"


def render_diff(ops: list[Op]) -> list[str]:
    """Non-matching rows with one row of context, equal runs collapsed."""
    show = [False] * len(ops)
    for i, op in enumerate(ops):
        if op.kind != "equal":
            for k in range(max(0, i - CONTEXT), min(len(ops), i + CONTEXT + 1)):
                show[k] = True
    out: list[str] = []
    hidden = 0
    for i, op in enumerate(ops):
        if not show[i]:
            hidden += 1
            continue
        if hidden:
            out.append(f"({hidden} matching lines collapsed)")
            hidden = 0
        left = f"{op.target.mnemonic:<5} {op.target.operands}" if op.target else ""
        right = f"{op.candidate.mnemonic:<5} {op.candidate.operands}" if op.candidate else ""
        label = _LABEL.get(op.kind, "")
        out.append(f"{i + 1:5d}  {left:<{_COL}}| {right:<{_COL}} {label}".rstrip())
    if hidden:
        out.append(f"({hidden} matching lines collapsed)")
    return out


def render_result(
    check: int, max_checks: int, current: float, best: float, results: list[VersionResult]
) -> str:
    """Full text for one check. Shows the first non-matching version."""
    if all(r.match for r in results):
        return render_match(check, max_checks)
    r = next(r for r in results if not r.match)
    lines = [header(check, max_checks, current, best, r)]
    body = render_diff(r.ops)
    if not body and r.reasons:
        body = [f"instructions identical, but: {reason}" for reason in r.reasons]
    hint_lines = ["hints:", *[f" - {text} (suggestion)" for _, text in r.hints]] if r.hints else []
    room = MAX_LINES - 1 - len(hint_lines)
    if len(body) > room:
        body = [*body[: room - 1], f"({len(body) - room + 1} more lines not shown)"]
    return "\n".join([*lines, *body, *hint_lines])
