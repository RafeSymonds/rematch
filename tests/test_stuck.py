"""The review queue reads hints and open issues from a failed attempt's sandbox."""

from __future__ import annotations

import json
from pathlib import Path

from rematch.db import store
from rematch.live.stuck import hint_tally, stuck_rows


def test_stuck_rows_report_best_check_hints_and_open_issues(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite")
    for fid, status in ((1, "failed"), (2, "matched"), (3, "stuck")):
        conn.execute(
            "INSERT INTO functions (id, name, address, module, tu_path, source_kind, mode,"
            " n_insns, status, attempts, updated_at)"
            " VALUES (?, ?, ?, 'ov1', 'asm/a.s', 'asm', 'thumb', 20, ?, 1, '2000-01-01')",
            (fid, f"f{fid}", fid * 0x10, status),
        )
    conn.execute(
        "INSERT INTO runs (id, kind, config_json, started_at) VALUES (1, 'live', '{}', '')"
    )
    sandbox = tmp_path / "sb"
    (sandbox / ".history").mkdir(parents=True)
    (sandbox / ".history" / "state.json").write_text(json.dumps({"best_check": 2}))
    (sandbox / ".history" / "002.json").write_text(
        json.dumps({"versions": {"v1": {"hints": ["pool_value"], "reasons": ["relocs differ"]}}})
    )
    (sandbox / "SUMMARY.json").write_text(json.dumps({"open_issues": "jump table"}))
    conn.execute(
        "INSERT INTO attempts (run_id, function_id, task_kind, outcome, best_score, cost_usd,"
        " sandbox_path) VALUES (1, 1, 'function', 'nonmatch', 91.5, 0.2, ?)",
        (str(sandbox),),
    )
    conn.commit()
    rows = stuck_rows(conn)
    assert [r.name for r in rows] == ["f1", "f3"]
    f1 = rows[0]
    assert (f1.best_score, f1.hints, f1.open_issues) == (91.5, ["pool_value"], "jump table")
    assert hint_tally(rows) == {"pool_value": 1, "(none)": 1}
    assert [r.name for r in stuck_rows(conn, hint="pool_value")] == ["f1"]
