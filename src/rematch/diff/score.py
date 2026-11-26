"""Weighted alignment of normalized lines and the 0-100 score (SPEC §6.5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rematch.diff.objdump_backend import Line

OpKind = Literal["equal", "operands", "opcode", "extra", "missing"]
_COST: dict[OpKind, float] = {
    "equal": 0.0,
    "operands": 0.5,
    "opcode": 1.0,
    "extra": 1.0,
    "missing": 1.0,
}


@dataclass(frozen=True)
class Op:
    kind: OpKind  # extra: only in the candidate; missing: only in the target
    target: Line | None
    candidate: Line | None


def _pair(t: Line, c: Line) -> OpKind:
    if t.text == c.text:
        return "equal"
    if t.mnemonic == c.mnemonic:
        return "operands"
    return "opcode"


def align(target: list[Line], candidate: list[Line]) -> list[Op]:
    """Minimum-cost alignment. Common prefix and suffix are matched first to keep the DP small."""
    pre = 0
    while pre < len(target) and pre < len(candidate) and target[pre].text == candidate[pre].text:
        pre += 1
    suf = 0
    while (
        suf < len(target) - pre
        and suf < len(candidate) - pre
        and target[-1 - suf].text == candidate[-1 - suf].text
    ):
        suf += 1
    t_mid = target[pre : len(target) - suf]
    c_mid = candidate[pre : len(candidate) - suf]

    n, m = len(t_mid), len(c_mid)
    dist = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dist[i][0] = float(i)
    for j in range(1, m + 1):
        dist[0][j] = float(j)
    for i in range(1, n + 1):
        row, prev = dist[i], dist[i - 1]
        t = t_mid[i - 1]
        for j in range(1, m + 1):
            sub = prev[j - 1] + _COST[_pair(t, c_mid[j - 1])]
            row[j] = min(sub, prev[j] + 1.0, row[j - 1] + 1.0)

    middle: list[Op] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            kind = _pair(t_mid[i - 1], c_mid[j - 1])
            if dist[i][j] == dist[i - 1][j - 1] + _COST[kind]:
                middle.append(Op(kind, t_mid[i - 1], c_mid[j - 1]))
                i, j = i - 1, j - 1
                continue
        if i > 0 and dist[i][j] == dist[i - 1][j] + 1.0:
            middle.append(Op("missing", t_mid[i - 1], None))
            i -= 1
        else:
            middle.append(Op("extra", None, c_mid[j - 1]))
            j -= 1
    middle.reverse()

    head = [Op("equal", t, c) for t, c in zip(target[:pre], candidate[:pre], strict=True)]
    tail_t = target[len(target) - suf :]
    tail_c = candidate[len(candidate) - suf :]
    tail = [Op("equal", t, c) for t, c in zip(tail_t, tail_c, strict=True)]
    return head + middle + tail


def score(ops: list[Op], n_target: int, n_candidate: int) -> float:
    longest = max(n_target, n_candidate)
    if longest == 0:
        return 100.0
    cost = sum(_COST[op.kind] for op in ops)
    return max(0.0, 100.0 * (1.0 - cost / longest))
