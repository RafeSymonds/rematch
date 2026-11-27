"""Frozen, size-stratified bench sets (SPEC §11.2)."""

from __future__ import annotations

import json
import random
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

# (name, lower bound inclusive, upper bound exclusive) on instruction count.
BUCKETS: tuple[tuple[str, int, float], ...] = (
    ("S", 0, 40),
    ("M", 40, 150),
    ("L", 150, float("inf")),
)


def bucket(n_insns: int) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= n_insns < hi:
            return name
    raise ValueError(n_insns)


def set_id(n: int, seed: int, cutoff: str, variant: str = "") -> str:
    return f"b{n}-s{seed}-c{cutoff.replace('-', '')}{variant}"


def quotas(sizes: dict[str, int], n: int) -> dict[str, int]:
    """Largest-remainder allocation of `n` proportional to bucket sizes."""
    total = sum(sizes.values())
    if total == 0:
        return {k: 0 for k in sizes}
    n = min(n, total)
    raw = {k: n * v / total for k, v in sizes.items()}
    out = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[: n - sum(out.values())]:
        out[k] += 1
    return out


def stratified_sample(
    sizes: dict[int, int],  # function id -> n_insns
    n: int,
    seed: int,
    eligible: Callable[[list[int]], list[int]],
) -> tuple[list[int], list[int]]:
    """Seeded sample, proportional per bucket, keeping only ids `eligible` accepts.

    Candidates are tried in a seeded order per bucket, in batches, until the bucket's quota is
    filled, so only about `n` functions need the (expensive) eligibility check. Returns the
    sample and the ids that were tried and rejected.
    """
    rng = random.Random(seed)
    by_bucket: dict[str, list[int]] = {name: [] for name, _, _ in BUCKETS}
    for fid in sorted(sizes):
        by_bucket[bucket(sizes[fid])].append(fid)
    quota = quotas({k: len(v) for k, v in by_bucket.items()}, n)
    chosen: list[int] = []
    rejected: list[int] = []
    for name, ids in by_bucket.items():
        order = ids[:]
        rng.shuffle(order)
        need = quota[name]
        pos = 0
        while need > 0 and pos < len(order):
            batch = order[pos : pos + need + 2]
            pos += len(batch)
            ok = set(eligible(batch))
            for fid in batch:
                if fid in ok and need > 0:
                    chosen.append(fid)
                    need -= 1
                elif fid not in ok:
                    rejected.append(fid)
    return sorted(chosen), rejected


def load_set(conn: sqlite3.Connection, sid: str) -> list[int] | None:
    row = conn.execute("SELECT function_ids_json FROM bench_sets WHERE id = ?", (sid,)).fetchone()
    return None if row is None else [int(x) for x in json.loads(row[0])]


def save_set(conn: sqlite3.Connection, sid: str, ids: list[int], seed: int, cutoff: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO bench_sets (id, function_ids_json, seed, cutoff, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, json.dumps(ids), seed, cutoff, datetime.now(UTC).isoformat(timespec="seconds")),
        )


def live_sample(conn: sqlite3.Connection, n: int, seed: int) -> list[int]:
    """`n` untried assembly functions, stratified by size bucket in proportion to what remains.

    For live pilots: the real work, with no human C and no human-written types for the target.
    """
    rows = conn.execute(
        "SELECT id, n_insns FROM functions"
        " WHERE source_kind = 'asm' AND status = 'untried' AND n_insns IS NOT NULL ORDER BY id"
    ).fetchall()
    by_bucket: dict[str, list[int]] = {name: [] for name, _, _ in BUCKETS}
    for fid, n_insns in rows:
        by_bucket[bucket(int(n_insns))].append(int(fid))
    total = sum(len(v) for v in by_bucket.values())
    if total == 0:
        return []
    n = min(n, total)
    exact = {k: n * len(v) / total for k, v in by_bucket.items()}
    quota = {k: int(x) for k, x in exact.items()}
    for k in sorted(exact, key=lambda k: exact[k] - quota[k], reverse=True)[
        : n - sum(quota.values())
    ]:
        quota[k] += 1
    rng = random.Random(seed)
    picked: list[int] = []
    for k, ids in by_bucket.items():
        picked += rng.sample(ids, quota[k])
    return sorted(picked)
