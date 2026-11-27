"""The review queue (SPEC §8.4, Phase 5): functions the live loop could not match.

For each failed or stuck function: its attempts, best score and cost, the hint categories at
the best check of its best attempt (from the sandbox's `.history/`), and what the worker said
was still open (`SUMMARY.json`). `accept_human` records a human's fix as a match after the same
check a worker's candidate gets.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from rematch.session import Session


@dataclass
class StuckRow:
    function_id: int
    name: str
    module: str
    tu: str
    status: str
    n_insns: int
    attempts: int
    best_score: float
    cost: float
    hints: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    open_issues: str = ""
    sandbox: str | None = None
    best_candidate: str | None = None


def _best_check(sandbox: Path) -> dict[str, object]:
    try:
        state = json.loads((sandbox / ".history" / "state.json").read_text())
        n = int(state.get("best_check") or 0)
        return dict(json.loads((sandbox / ".history" / f"{n:03d}.json").read_text()))
    except (OSError, ValueError):
        return {}


def _summary(sandbox: Path) -> str:
    try:
        data = json.loads((sandbox / "SUMMARY.json").read_text())
    except (OSError, ValueError):
        return ""
    return str(data.get("open_issues") or "") if isinstance(data, dict) else ""


def stuck_rows(
    conn: sqlite3.Connection, module: str | None = None, hint: str | None = None
) -> list[StuckRow]:
    query = (
        "SELECT f.id, f.name, f.module, f.tu_path, f.status, f.n_insns FROM functions f"
        " WHERE f.status IN ('failed', 'stuck')"
    )
    args: list[object] = []
    if module:
        query += " AND f.module = ?"
        args.append(module)
    out: list[StuckRow] = []
    for fid, name, mod, tu, status, n_insns in conn.execute(query + " ORDER BY f.address", args):
        rows = conn.execute(
            "SELECT COUNT(*), MAX(best_score), SUM(cost_usd) FROM attempts"
            " WHERE function_id = ? AND task_kind = 'function'",
            (fid,),
        ).fetchone()
        best = conn.execute(
            "SELECT sandbox_path, best_candidate_path FROM attempts WHERE function_id = ?"
            " AND task_kind = 'function' ORDER BY best_score DESC, id DESC LIMIT 1",
            (fid,),
        ).fetchone()
        row = StuckRow(
            int(fid),
            str(name),
            str(mod),
            str(tu),
            str(status),
            int(n_insns or 0),
            int(rows[0] or 0),
            float(rows[1] or 0.0),
            float(rows[2] or 0.0),
        )
        if best and best[0]:
            sandbox = Path(best[0])
            check = _best_check(sandbox)
            versions = check.get("versions")
            for v in versions.values() if isinstance(versions, dict) else []:
                row.hints += [h for h in v.get("hints", []) if h not in row.hints]
                row.reasons += [r for r in v.get("reasons", []) if r not in row.reasons]
            if check.get("reason"):
                row.reasons.append(str(check["reason"]))
            row.open_issues = _summary(sandbox)
            row.sandbox, row.best_candidate = str(sandbox), best[1]
        if hint and hint not in row.hints:
            continue
        out.append(row)
    return out


def hint_tally(rows: list[StuckRow]) -> dict[str, int]:
    tally: dict[str, int] = {}
    for r in rows:
        for h in r.hints or ["(none)"]:
            tally[h] = tally.get(h, 0) + 1
    return dict(sorted(tally.items(), key=lambda kv: -kv[1]))


def accept_human(s: Session, name: str, source: str) -> tuple[bool, str]:
    """Check a human's C for `name` like a worker's candidate; on a match, record it (model
    'human') and queue a learnings candidate that points the curator at the difference."""
    import shutil

    from rematch.db import store
    from rematch.worker.attempt import _record_types, finish_run, run_config, start_run
    from rematch.worker.check_entry import evaluate
    from rematch.worker.recheck import _record_match
    from rematch.worker.sandbox import SandboxOptions, build_sandbox, load_functions
    from rematch.worker.task import Task

    conn = store.connect(s.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM functions WHERE name = ? AND source_kind = 'asm'", (name,)
        ).fetchone()
        if row is None:
            return False, f"no assembly function named {name}"
        fn = load_functions(conn, [int(row[0])])[0]
        run_id = start_run(conn, "live", run_config(s, "human", "none", {"human": name}), None)
        sb = s.settings.rematch_workdir.expanduser() / "human" / str(run_id)
        shutil.rmtree(sb, ignore_errors=True)
        build_sandbox(s, fn, sb, SandboxOptions())
        task = Task.load(sb / ".control")
        rejected, errors, results = evaluate(sb, task, source, sb / ".build", sb / ".control")
        if rejected or errors or not results or not all(r.match for r in results):
            finish_run(conn, run_id, notes=f"human fix for {name}: no match")
            why = rejected or (errors[0] if errors else "")
            if not why and results:
                why = f"score {min(r.score for r in results):.1f}"
            return False, why or "no result"
        (sb / "candidate.c").write_text(source, encoding="utf-8")
        attempt_id = _record_match(
            conn, run_id, fn.id, source, sb, [r.version for r in results], model="human"
        )
        _record_types(conn, sb, task, fn.id, attempt_id, source)
        with conn:
            conn.execute(
                "INSERT INTO learning_candidates (attempt_id, text, created_at)"
                " VALUES (?, ?, datetime('now'))",
                (
                    attempt_id,
                    f"A human matched {name} after the workers failed. Compare the accepted"
                    f" source (attempt {attempt_id}) with the workers' best candidates and"
                    " state the rule that made the difference, if there is a general one.",
                ),
            )
        finish_run(conn, run_id, notes=f"human fix for {name}")
        return True, f"attempt {attempt_id}"
    finally:
        conn.close()
