"""`rematch recheck`: re-verify earlier non-matching attempts with the current checker.

A checker fix (padding, data naming, normalization) can turn an attempt that was scored as a
miss into a match without any new model call. For every non-matching attempt of a live run,
this rebuilds the sandbox, compiles the attempt's best candidate with `evaluate` and, when every
version matches, records a zero-cost attempt in a new live run (notes: recheck) and the match.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rematch.db import store
from rematch.lint.readability import lint
from rematch.session import Session
from rematch.worker.attempt import _record_types, finish_run, now, run_config, start_run
from rematch.worker.check_entry import evaluate
from rematch.worker.sandbox import SandboxError, SandboxOptions, build_sandbox, load_functions
from rematch.worker.task import Task

Log = Callable[[str], None]


@dataclass
class Rechecked:
    attempt_id: int
    function: str
    matched: bool
    detail: str


def _candidate(best: str | None, sandbox: str | None) -> str | None:
    for p in (best, sandbox and str(Path(sandbox) / "candidate.c")):
        if p and Path(p).is_file():
            return Path(p).read_text(encoding="utf-8", errors="replace")
    return None


def recheck_run(s: Session, run_id: int, log: Log) -> tuple[int | None, list[Rechecked]]:
    conn = store.connect(s.db_path)
    try:
        kind = conn.execute("SELECT kind FROM runs WHERE id = ?", (run_id,)).fetchone()
        if kind is None or kind[0] != "live":
            raise ValueError(f"run {run_id} is not a live run")
        rows = conn.execute(
            "SELECT a.id, a.function_id, a.best_candidate_path, a.sandbox_path FROM attempts a"
            " JOIN functions f ON f.id = a.function_id"
            " WHERE a.run_id = ? AND a.task_kind = 'function' AND a.outcome != 'match'"
            " AND f.status NOT IN ('matched', 'integrated') ORDER BY a.id",
            (run_id,),
        ).fetchall()
        if not rows:
            return None, []
        new_run = start_run(
            conn, "live", run_config(s, "recheck", "none", {"recheck_of": run_id}), None
        )
        out: list[Rechecked] = []
        root = s.settings.rematch_workdir.expanduser() / "recheck" / str(new_run)
        for attempt_id, fid, best, sandbox in rows:
            fn = load_functions(conn, [int(fid)])[0]
            source = _candidate(best, sandbox)
            if source is None:
                out.append(Rechecked(attempt_id, fn.name, False, "no candidate on disk"))
                continue
            sb = root / str(attempt_id)
            shutil.rmtree(sb, ignore_errors=True)
            try:
                build_sandbox(s, fn, sb, SandboxOptions())
            except SandboxError as exc:
                out.append(Rechecked(attempt_id, fn.name, False, f"sandbox: {exc}"))
                continue
            control = sb / ".control"
            task = Task.load(control)
            rejected, errors, results = evaluate(sb, task, source, sb / ".build", control)
            if rejected or errors or not results or not all(r.match for r in results):
                why = (
                    rejected
                    or (errors[0] if errors else "")
                    or (f"score {min(r.score for r in results):.1f}" if results else "no result")
                )
                out.append(Rechecked(attempt_id, fn.name, False, why))
                continue
            (sb / "candidate.c").write_text(source, encoding="utf-8")
            new_id = _record_match(conn, new_run, fn.id, source, sb, [r.version for r in results])
            n = _record_types(conn, sb, task, fn.id, new_id, source)
            out.append(
                Rechecked(attempt_id, fn.name, True, f"attempt {new_id}, {n} provisional type(s)")
            )
            log(f"recheck {fn.name}: match (attempt {attempt_id} -> {new_id})")
        finish_run(conn, new_run, notes=f"recheck of run {run_id}")
        return new_run, out
    finally:
        conn.close()


def _record_match(
    conn: sqlite3.Connection,
    run_id: int,
    fn_id: int,
    source: str,
    sandbox: Path,
    versions: list[str],
    model: str = "recheck",
) -> int:
    stamp = now()
    with conn:
        cur = conn.execute(
            "INSERT INTO attempts (run_id, function_id, task_kind, model, effort, started_at,"
            " ended_at, outcome, checks_used, best_score, final_score, num_turns, cost_usd,"
            " sandbox_path, best_candidate_path)"
            " VALUES (?, ?, 'function', ?, 'none', ?, ?, 'match', 1, 100, 100, 0, 0,"
            " ?, ?)",
            (run_id, fn_id, model, stamp, stamp, str(sandbox), str(sandbox / "candidate.c")),
        )
        attempt_id = int(cur.lastrowid or 0)
        conn.execute(
            "UPDATE functions SET status = 'matched', best_score = 100, updated_at = ?"
            " WHERE id = ?",
            (stamp, fn_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO matches (function_id, attempt_id, c_source,"
            " verified_versions, lint_json, verified_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                fn_id,
                attempt_id,
                source,
                ",".join(versions),
                lint(source).to_json(),
                stamp,
            ),
        )
    return attempt_id
