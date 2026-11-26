"""Example selection (SPEC §6.7). Phase 2 baseline: naive, by mode and size.

Similarity ranking replaces this in Phase 3 as a measured ablation.
"""

from __future__ import annotations

import random
import sqlite3

from rematch.worker.sandbox import FunctionRecord, find_function


def naive_examples(
    conn: sqlite3.Connection, target: FunctionRecord, k: int, seed: int
) -> tuple[FunctionRecord, ...]:
    """`k` matched human-C functions with the same mode and the nearest instruction count.

    Never from the target's own TU (§6.7). Ties are broken by a seeded shuffle.
    """
    if k <= 0:
        return ()
    rows = conn.execute(
        "SELECT module, address, n_insns FROM functions WHERE source_kind = 'c_human' "
        "AND status != 'skipped' AND mode = ? AND tu_path != ? AND id != ?",
        (target.mode, target.tu_path, target.id),
    ).fetchall()
    size = conn.execute("SELECT n_insns FROM functions WHERE id = ?", (target.id,)).fetchone()[0]
    rng = random.Random(f"{seed}:{target.id}")
    rng.shuffle(rows)
    rows.sort(key=lambda r: abs((r[2] or 0) - (size or 0)))
    return tuple(find_function(conn, f"{m}:{a:#x}") for m, a, _ in rows[:k])
