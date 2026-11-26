"""Bench pools (SPEC §11.1): clean vs possibly contaminated, pre-screened for isolated mode.

Pre-screen: a function is eligible only if its own human C matches in isolated mode. The few
that need TU context cannot be matched by any worker in isolated mode, so they would only add
noise. Results are cached per pin and context version.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable

from rematch.context import CONTEXT_VERSION
from rematch.session import Session
from rematch.worker.roundtrip import roundtrip
from rematch.worker.sandbox import find_function

Log = Callable[[str], None]


class PrescreenError(Exception):
    pass


def pool_sizes(conn: sqlite3.Connection, pool: str, cutoff: str) -> dict[int, int]:
    """function id -> n_insns for the clean or contaminated human-C pool."""
    clause = (
        "decompiled_at >= ?" if pool == "clean" else "(decompiled_at IS NULL OR decompiled_at < ?)"
    )
    rows = conn.execute(
        "SELECT id, n_insns FROM functions WHERE source_kind = 'c_human' AND status != 'skipped' "
        f"AND {clause}",
        (cutoff,),
    ).fetchall()
    return {int(fid): int(n or 0) for fid, n in rows}


class Prescreen:
    """Isolated-mode round-trip results, cached in `var/<target>.prescreen.json`."""

    def __init__(self, s: Session, conn: sqlite3.Connection, log: Log, workers: int) -> None:
        self.s, self.conn, self.log, self.workers = s, conn, log, workers
        self.path = s.settings.rematch_var / f"{s.definition.name}.prescreen.json"
        self.key = f"{s.settings.target_pin}:{CONTEXT_VERSION}"  # rebuilt when either changes
        data = json.loads(self.path.read_text()) if self.path.is_file() else {}
        self.results: dict[str, str] = data.get(self.key, {})
        self.root = s.settings.rematch_workdir.expanduser() / "prescreen"

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({self.key: self.results}, indent=1), encoding="utf-8")

    def eligible(self, ids: list[int]) -> list[int]:
        todo = [fid for fid in ids if str(fid) not in self.results]
        if todo:
            marks = ",".join("?" * len(todo))
            rows = self.conn.execute(
                f"SELECT id, module, address FROM functions WHERE id IN ({marks})", todo
            ).fetchall()
            fns = [find_function(self.conn, f"{m}:{a:#x}") for _, m, a in rows]
            batch = self.root / f"batch_{fns[0].id}"
            outcomes = roundtrip(self.s, fns, batch, self.log, self.workers)
            for fn, out in zip(fns, outcomes, strict=True):
                if out.status == "error":  # a harness problem, not a property of the function
                    raise PrescreenError(f"pre-screen of {fn.name} failed: {out.detail[:2]}")
                if out.status == "context_error":
                    self.log(f"{fn.name}: context command failed; ineligible ({out.detail[:1]})")
                self.results[str(fn.id)] = out.status
            self._save()
            shutil.rmtree(batch, ignore_errors=True)  # sandboxes hold the human C; don't keep them
        return [fid for fid in ids if self.results.get(str(fid)) == "match"]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for status in self.results.values():
            counts[status] = counts.get(status, 0) + 1
        return counts
