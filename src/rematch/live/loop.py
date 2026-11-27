"""`rematch run`: the unattended live loop (SPEC §8, Phase 4a).

Workers take untried and retry-ready assembly functions, TUs closest to completion first and
each TU in address order, so matches form runs the integrator can land. A function is leased
while an attempt runs (heartbeat every 30 s; expired leases revert at start). A failed function
moves up ESCALATION_LADDER after RETRY_COOLDOWN_SEC with its best candidate as previous_best.c;
past the last rung it is `stuck`. Every `integrate_every` matches, ready TUs are integrated in
a background thread. Ctrl-C stops new launches and lets running attempts finish.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rematch.bench.run import _Gate
from rematch.context.examples import similar_examples
from rematch.db import store
from rematch.session import Session
from rematch.worker.attempt import (
    AttemptSpec,
    finish_run,
    run_attempt,
    run_config,
    start_run,
    system_prompt_file,
)
from rematch.worker.outcome import Classified
from rematch.worker.sandbox import FunctionRecord, SandboxOptions, load_functions

Log = Callable[[str], None]
HEARTBEAT_SEC = 30


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def ladder(s: Session) -> list[tuple[str, str]]:
    rungs = [r.split(":", 1) for r in s.settings.escalation_ladder.split(",") if ":" in r]
    return [(m.strip(), e.strip()) for m, e in rungs] or [
        (s.settings.worker_model, s.settings.worker_effort)
    ]


def _dead_local_owners(conn: sqlite3.Connection) -> list[str]:
    """Lease owners on this host (`host:pid`) whose process no longer exists."""
    host = socket.gethostname()
    dead = []
    for (owner,) in conn.execute(
        "SELECT DISTINCT lease_owner FROM functions WHERE status = 'leased'"
    ):
        if str(owner or "").rpartition(":")[0] == host and _owner_dead(str(owner)):
            dead.append(str(owner))
    return dead


def _owner_dead(owner: str) -> bool:
    h, _, pid = owner.rpartition(":")
    if h != socket.gethostname() or not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def recover_runs(conn: sqlite3.Connection) -> int:
    """Close live-loop runs whose owner process died: their open attempts become errors."""
    closed = 0
    stamp = _iso(datetime.now(UTC))
    for run_id, config in conn.execute(
        "SELECT id, config_json FROM runs WHERE kind = 'live' AND ended_at IS NULL"
    ).fetchall():
        try:
            owner = json.loads(config).get("owner")
        except json.JSONDecodeError:
            continue
        if not owner or not _owner_dead(str(owner)):
            continue
        with conn:
            conn.execute(
                "UPDATE attempts SET outcome = 'error', ended_at = ? WHERE run_id = ?"
                " AND ended_at IS NULL",
                (stamp, run_id),
            )
            conn.execute(
                "UPDATE runs SET ended_at = ?, notes = 'owner process died' WHERE id = ?",
                (stamp, run_id),
            )
        closed += 1
    return closed


def recover_leases(conn: sqlite3.Connection) -> int:
    """Expired leases, and leases of dead processes on this host, go back to untried (no
    attempt yet) or failed."""
    stamp = _iso(datetime.now(UTC))
    dead = _dead_local_owners(conn)
    marks = ",".join("?" * len(dead)) or "NULL"
    with conn:
        cur = conn.execute(
            "UPDATE functions SET status = CASE WHEN attempts = 0 THEN 'untried' ELSE 'failed'"
            " END, lease_owner = NULL, lease_expires = NULL WHERE status = 'leased' AND"
            f" (lease_expires IS NULL OR lease_expires < ? OR lease_owner IN ({marks}))",
            (stamp, *dead),
        )
    return cur.rowcount


def pick_order(conn: sqlite3.Connection, n_rungs: int, cooldown: float) -> list[tuple[int, int]]:
    """(function id, rung) in work order: TUs with the fewest unmatched functions first."""
    ready_before = _iso(datetime.now(UTC) - timedelta(seconds=cooldown))
    rows = conn.execute(
        "SELECT f.id, f.tu_path, f.address, f.escalation, t.left FROM functions f"
        " JOIN (SELECT tu_path, SUM(status NOT IN ('matched','integrated')) AS left"
        "       FROM functions WHERE source_kind = 'asm' GROUP BY tu_path) t"
        "   ON t.tu_path = f.tu_path"
        " WHERE f.source_kind = 'asm' AND (f.status = 'untried'"
        "   OR (f.status = 'failed' AND f.escalation < ? AND f.updated_at < ?))"
        " ORDER BY t.left, f.tu_path, f.address",
        (n_rungs, ready_before),
    ).fetchall()
    return [(int(fid), int(esc)) for fid, _tu, _addr, esc, _left in rows]


def acquire(conn: sqlite3.Connection, fid: int, owner: str, ttl: float) -> bool:
    with conn:
        cur = conn.execute(
            "UPDATE functions SET status = 'leased', lease_owner = ?, lease_expires = ?"
            " WHERE id = ? AND status IN ('untried', 'failed')",
            (owner, _iso(datetime.now(UTC) + timedelta(seconds=ttl)), fid),
        )
    return cur.rowcount == 1


def settle(conn: sqlite3.Connection, fid: int, c: Classified | None, n_rungs: int) -> None:
    """After an attempt: clear the lease; a miss climbs the ladder or becomes stuck."""
    with conn:
        if c is None:  # harness error before the attempt recorded anything
            conn.execute(
                "UPDATE functions SET status = CASE WHEN attempts = 0 THEN 'untried'"
                " ELSE 'failed' END WHERE id = ? AND status = 'leased'",
                (fid,),
            )
        elif c.outcome != "match":
            conn.execute(
                "UPDATE functions SET escalation = escalation + 1, status = CASE"
                " WHEN escalation + 1 >= ? THEN 'stuck' ELSE 'failed' END WHERE id = ?",
                (n_rungs, fid),
            )
        conn.execute(
            "UPDATE functions SET lease_owner = NULL, lease_expires = NULL WHERE id = ?", (fid,)
        )


def previous_best(conn: sqlite3.Connection, fid: int) -> tuple[str, float] | None:
    row = conn.execute(
        "SELECT best_candidate_path, best_score FROM attempts WHERE function_id = ?"
        " AND task_kind = 'function' AND best_candidate_path IS NOT NULL"
        " ORDER BY best_score DESC, id DESC LIMIT 1",
        (fid,),
    ).fetchone()
    if row is None or not Path(row[0]).is_file():
        return None
    return Path(row[0]).read_text(encoding="utf-8", errors="replace"), float(row[1] or 0)


@dataclass
class LoopStats:
    run_id: int = 0
    launched: int = 0
    matched: int = 0
    failed: int = 0
    errors: int = 0
    integrated: list[str] = field(default_factory=list)


def run_live(
    s: Session,
    *,
    workers: int,
    max_attempts: int | None,
    until: datetime | None,
    integrate_every: int,
    integrate_fn: Callable[[], list[str]] | None,
    log: Log,
) -> LoopStats:
    rungs = ladder(s)
    owner = f"{socket.gethostname()}:{os.getpid()}"
    ttl = s.settings.attempt_timeout_sec + 300
    conn = store.connect(s.db_path)
    stats = LoopStats()
    try:
        if n := recover_leases(conn):
            log(f"recovered {n} expired lease(s)")
        if n := recover_runs(conn):
            log(f"closed {n} run(s) whose owner process died")
        stats.run_id = start_run(
            conn, "live", run_config(s, "ladder", "ladder", {"loop": True, "owner": owner}), None
        )
    finally:
        conn.close()
    run_dir = s.settings.rematch_var / "runs" / str(stats.run_id)
    prompt_file = system_prompt_file(s, run_dir)
    gate = _Gate()
    stop = threading.Event()
    done_heartbeat = threading.Event()

    def on_sigint(signum: int, frame: object) -> None:
        if stop.is_set():
            raise KeyboardInterrupt
        log("stopping: no new attempts; waiting for running ones (Ctrl-C again to abort)")
        stop.set()

    previous = signal.signal(signal.SIGINT, on_sigint)

    def heartbeat() -> None:
        hconn = store.connect(s.db_path)
        try:
            while not done_heartbeat.wait(HEARTBEAT_SEC):
                with hconn:
                    hconn.execute(
                        "UPDATE functions SET lease_expires = ? WHERE lease_owner = ?"
                        " AND status = 'leased'",
                        (_iso(datetime.now(UTC) + timedelta(seconds=ttl)), owner),
                    )
        finally:
            hconn.close()

    integrating: threading.Thread | None = None

    def integrate_bg() -> None:
        if integrate_fn is None:
            return
        try:
            stats.integrated += integrate_fn()
        except Exception as exc:  # integration problems must not stop matching
            log(f"integration error: {exc!r}")

    def one(fn: FunctionRecord, rung: int) -> tuple[FunctionRecord, Classified | None]:
        gate.wait()
        model, effort = rungs[min(rung, len(rungs) - 1)]
        wconn = store.connect(s.db_path)
        try:
            examples = tuple(similar_examples(s, wconn, fn, s.settings.examples_k, False, log))
            prev = previous_best(wconn, fn.id) if rung > 0 else None
        finally:
            wconn.close()
        spec = AttemptSpec(
            run_id=stats.run_id,
            kind="live",
            fn=fn,
            options=SandboxOptions(examples=examples, previous_best=prev),
            model=model,
            effort=effort,
            system_prompt=prompt_file,
            gate=gate,
            escalation=rung,
        )
        try:
            _, c = run_attempt(s, spec, log)
            return fn, c
        except Exception as exc:
            log(f"{fn.name}: harness error {exc!r}")
            return fn, None

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()
    conn = store.connect(s.db_path)
    since_integration = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            running: dict[Future[tuple[FunctionRecord, Classified | None]], int] = {}
            queue: list[tuple[int, int]] = []
            while True:
                launching = not stop.is_set()
                if until is not None and datetime.now(UTC) >= until:
                    launching = False
                if max_attempts is not None and stats.launched >= max_attempts:
                    launching = False
                while launching and len(running) < workers:
                    if not queue:
                        if n := recover_leases(conn):
                            log(f"recovered {n} expired lease(s)")
                        queue = pick_order(conn, len(rungs), s.settings.retry_cooldown_sec)
                        queue = [q for q in queue if q[0] not in running.values()]
                        if not queue:
                            break
                    fid, rung = queue.pop(0)
                    if not acquire(conn, fid, owner, ttl):
                        continue
                    fn = load_functions(conn, [fid])[0]
                    running[pool.submit(one, fn, rung)] = fid
                    stats.launched += 1
                    if max_attempts is not None and stats.launched >= max_attempts:
                        break
                if not running:
                    break
                finished, _ = wait(list(running), timeout=5, return_when=FIRST_COMPLETED)
                for fut in finished:
                    fid = running.pop(fut)
                    fn, c = fut.result()
                    settle(conn, fid, c, len(rungs))
                    if c is None:
                        stats.errors += 1
                    elif c.outcome == "match":
                        stats.matched += 1
                        since_integration += 1
                    else:
                        stats.failed += 1
                if (
                    integrate_fn is not None
                    and since_integration >= integrate_every
                    and (integrating is None or not integrating.is_alive())
                ):
                    since_integration = 0
                    integrating = threading.Thread(target=integrate_bg, daemon=True)
                    integrating.start()
        if integrating is not None:
            integrating.join()
        if integrate_fn is not None and since_integration:
            integrate_bg()
    finally:
        done_heartbeat.set()
        signal.signal(signal.SIGINT, previous)
        notes = (
            f"launched {stats.launched}, matched {stats.matched}, failed {stats.failed},"
            f" errors {stats.errors}, integrated {len(stats.integrated)} TU step(s)"
        )
        finish_run(conn, stats.run_id, notes=notes)
        conn.close()
        log(notes)
    return stats
