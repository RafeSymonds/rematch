"""Example selection (SPEC §6.7). Phase 2 baseline: naive, by mode and size.

Similarity ranking replaces this in Phase 3 as a measured ablation.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Callable
from pathlib import Path

from rematch.context.similarity import Features, build_pool_features, load_cache, rank, save_cache
from rematch.session import Session
from rematch.toolchain.compile_db import CompileDB, CompileDBError
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


FEATURES_VERSION = 1


def _pool_cache(
    s: Session, conn: sqlite3.Connection, log: Callable[[str], None]
) -> dict[int, Features]:
    """Similarity features of every human-C function at the pin (cached in REMATCH_VAR)."""
    path = s.settings.rematch_var / f"{s.definition.name}.similarity.json"
    key = f"{s.settings.target_pin}:{FEATURES_VERSION}"
    cached = load_cache(path, key)
    if cached is not None:
        return cached
    build = s.definition.require(s.definition.build_info, "build")
    db = CompileDB.load(
        s.definition.build_dir(s.worktree, s.primary_version) / build.compile_commands, s.worktree
    )
    rows = conn.execute(
        "SELECT id, tu_path, name FROM functions"
        " WHERE source_kind = 'c_human' AND status != 'skipped'"
    ).fetchall()
    objects: dict[str, Path] = {}
    for _, tu, _ in rows:
        if tu not in objects:
            try:
                objects[tu] = db.entry(tu).object_path
            except CompileDBError:
                continue
    pool = build_pool_features(
        s.settings.objdump, objects, {int(fid): (tu, name) for fid, tu, name in rows}, log
    )
    save_cache(path, key, pool)
    return pool


def similar_examples(
    s: Session,
    conn: sqlite3.Connection,
    target: FunctionRecord,
    k: int,
    bench: bool,
    log: Callable[[str], None],
) -> tuple[FunctionRecord, ...]:
    """`k` most similar matched human-C functions (SPEC §6.7).

    Never from the target's own TU. In bench mode also never one decompiled at the same time as
    the target or later: it wasn't available to the person who decompiled the target, and a
    sibling from the same change would hand the worker the answer's twin.
    """
    if k <= 0:
        return ()
    pool = _pool_cache(s, conn, log)
    if target.id in pool:
        target_features = pool[target.id]
    else:  # a live assembly target: features from its own vanilla object
        build = s.definition.require(s.definition.build_info, "build")
        db = CompileDB.load(
            s.definition.build_dir(s.worktree, s.primary_version) / build.compile_commands,
            s.worktree,
        )
        part = build_pool_features(
            s.settings.objdump,
            {target.tu_path: db.entry(target.tu_path).object_path},
            {target.id: (target.tu_path, target.name)},
            lambda _m: None,
        )
        if target.id not in part:
            return ()
        target_features = part[target.id]
    excluded = {
        int(fid)
        for (fid,) in conn.execute("SELECT id FROM functions WHERE tu_path = ?", (target.tu_path,))
    }
    if bench:
        when = conn.execute(
            "SELECT decompiled_at FROM functions WHERE id = ?", (target.id,)
        ).fetchone()[0]
        if when is not None:
            excluded |= {
                int(fid)
                for (fid,) in conn.execute(
                    "SELECT id FROM functions"
                    " WHERE decompiled_at IS NOT NULL AND decompiled_at >= ?",
                    (when,),
                )
            }
    ids = rank(target_features, pool, excluded, k)
    out: list[FunctionRecord] = []
    for fid in ids:
        module, address = conn.execute(
            "SELECT module, address FROM functions WHERE id = ?", (fid,)
        ).fetchone()
        out.append(find_function(conn, f"{module}:{address:#x}"))
    return tuple(out)
