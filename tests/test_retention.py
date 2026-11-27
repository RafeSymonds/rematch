"""Attempt sandboxes are pruned once nothing will read them again."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rematch.db import store
from rematch.worker.attempt import prune_sandboxes


def test_prune_keeps_latest_and_best_until_matched(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite")
    conn.execute(
        "INSERT INTO functions (id, name, address, module, tu_path, source_kind, mode, n_insns,"
        " status, attempts, updated_at) VALUES (1, 'f', 16, 'm', 'asm/a.s', 'asm', 'thumb', 9,"
        " 'failed', 3, '')"
    )
    conn.execute(
        "INSERT INTO runs (id, kind, config_json, started_at) VALUES (1, 'live', '{}', '')"
    )
    work = tmp_path / "work"
    for aid, score in ((1, 90.0), (2, 95.0), (3, 80.0)):
        (work / "attempts" / str(aid)).mkdir(parents=True)
        conn.execute(
            "INSERT INTO attempts (id, run_id, function_id, task_kind, best_score, ended_at,"
            " sandbox_path) VALUES (?, 1, 1, 'function', ?, 'x', ?)",
            (aid, score, str(work / "attempts" / str(aid))),
        )
    conn.commit()
    s: Any = SimpleNamespace(settings=SimpleNamespace(rematch_workdir=work))
    assert prune_sandboxes(s, conn, 1) == 1
    assert sorted(p.name for p in (work / "attempts").iterdir()) == ["2", "3"]  # best, latest
    conn.execute(
        "INSERT INTO matches (function_id, attempt_id, c_source, verified_versions, lint_json,"
        " verified_at) VALUES (1, 3, 'x', 'v', '{}', '')"
    )
    conn.commit()
    assert prune_sandboxes(s, conn, 1) == 2
    assert not any((work / "attempts").iterdir())
