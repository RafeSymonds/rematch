"""One attempt end to end: sandbox, seal, worker, classification, records (SPEC §7, §5)."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from rematch import prompts
from rematch.context import CONTEXT_VERSION
from rematch.db import store
from rematch.lint.readability import lint
from rematch.session import Session
from rematch.types.probe import measure, signature
from rematch.types.provisional import TypesError, extract_block, parse_block
from rematch.worker.invoke import InvokeConfig, InvokeResult, rate_limit_wait, run_worker
from rematch.worker.outcome import Classified, classify, note_bullets
from rematch.worker.sandbox import FunctionRecord, SandboxOptions, build_sandbox, seal

Log = Callable[[str], None]
REMATCH_ROOT = Path(__file__).resolve().parents[3]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def git_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip()


def run_config(s: Session, model: str, effort: str, extra: dict[str, object]) -> str:
    settings = s.settings.model_dump(mode="json", exclude={"anthropic_api_key"})
    toml = (s.settings.target_repo / ".rematch" / "target.toml").read_bytes()
    return json.dumps(
        {
            "settings": settings,
            "model": model,
            "effort": effort,
            "prompt_version": prompts.prompt_version(),
            "context_version": CONTEXT_VERSION,
            "rematch_sha": git_sha(REMATCH_ROOT),
            "target_pin": s.settings.target_pin,
            "target_toml_sha256": __import__("hashlib").sha256(toml).hexdigest(),
            **extra,
        },
        default=str,
    )


def start_run(conn: sqlite3.Connection, kind: str, config_json: str, bench_set: str | None) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (kind, bench_set, config_json, started_at) VALUES (?, ?, ?, ?)",
            (kind, bench_set, config_json, now()),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, notes: str | None = None) -> None:
    with conn:
        conn.execute("UPDATE runs SET ended_at = ?, notes = ? WHERE id = ?", (now(), notes, run_id))


def system_prompt_file(s: Session, run_dir: Path) -> Path:
    path = run_dir / "worker_system.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompts.worker_system(s.definition.worker.intro), encoding="utf-8")
    return path


def deny_roots(s: Session) -> tuple[Path, ...]:
    """Places a worker must never read: answers, the harness, other answer-bearing sandboxes."""
    work = s.settings.rematch_workdir.expanduser()
    roots = [
        s.settings.target_repo,
        s.worktree,
        REMATCH_ROOT,
        s.settings.rematch_var.resolve(),
        work / "roundtrip",
        work / "prescreen",
        work / "selftest",
    ]
    roots += [
        Path(p.strip()).expanduser() for p in s.settings.reference_repos.split(",") if p.strip()
    ]
    return tuple(dict.fromkeys(roots))


MAX_RESUMES = 5
CONTINUE_PROMPT = (
    "Your previous session was interrupted by a rate limit. Continue the task. candidate.c, "
    "NOTES.md and .history/ are as you left them, and ./check keeps its count."
)


class Gate(Protocol):
    def pause(self, seconds: float) -> None: ...
    def wait(self) -> None: ...


class _NoGate:
    def pause(self, seconds: float) -> None:
        time.sleep(seconds)

    def wait(self) -> None:
        return None


@dataclass
class Totals:
    segments: int = 0
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0

    def add(self, r: InvokeResult) -> None:
        self.segments += 1
        self.turns += r.num_turns or 0
        self.input_tokens += r.usage("input_tokens")
        self.output_tokens += r.usage("output_tokens")
        self.cache_read_tokens += r.usage("cache_read_input_tokens")
        self.cost_usd += r.cost_usd or 0.0
        self.seconds += r.seconds


def _run_segments(
    cfg: InvokeConfig, sandbox: Path, stream: Path, spec: AttemptSpec, log: Log
) -> tuple[InvokeResult, Totals]:
    """Run the worker; after a rate limit, wait (shared gate) and resume the same session.

    Turn and wall-time budgets carry over across segments. If the session can't be resumed,
    a fresh session continues in the same sandbox, where candidate.c and .history/ survive.
    """
    gate = spec.gate or _NoGate()
    totals = Totals()
    prompt = (sandbox / "TASK.md").read_text(encoding="utf-8")
    session: str | None = None
    for segment in range(MAX_RESUMES + 1):
        seg_cfg = replace(
            cfg,
            max_turns=max(1, cfg.max_turns - totals.turns),
            timeout_sec=max(60.0, cfg.timeout_sec - totals.seconds),
        )
        path = stream if segment == 0 else stream.with_suffix(f".r{segment}.jsonl")
        result = run_worker(seg_cfg, sandbox, prompt, path, resume=session)
        if session and result.result is None and "No conversation found" in result.stderr_tail:
            log(f"{spec.fn.name}: session {session[:8]} not resumable; continuing fresh")
            prompt = CONTINUE_PROMPT + "\n\n" + (sandbox / "TASK.md").read_text(encoding="utf-8")
            result = run_worker(seg_cfg, sandbox, prompt, path.with_suffix(".fresh.jsonl"))
        totals.add(result)
        wait = rate_limit_wait(result, time.time(), segment)
        if wait is None or segment == MAX_RESUMES:
            return result, totals
        log(f"{spec.fn.name}: rate limited; resuming in {wait:.0f}s (pausing new attempts)")
        gate.pause(wait)
        gate.wait()
        session = result.session_id or session
        prompt = CONTINUE_PROMPT
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class AttemptSpec:
    run_id: int
    kind: str  # bench | live | selftest
    fn: FunctionRecord
    options: SandboxOptions
    model: str
    effort: str
    system_prompt: Path
    claude_bin: str | None = None  # override (selftest fakes)
    gate: Gate | None = None  # shared pause after rate limits (bench/run)
    escalation: int = 0  # ladder rung (live loop, SPEC §8.4)


def _record_types(
    conn: sqlite3.Connection, sandbox: Path, task: object, fn_id: int, attempt_id: int, source: str
) -> int:
    try:
        body = extract_block(source)
        if body is None:
            return 0
        types = parse_block(body)
    except TypesError:
        return 0
    from rematch.worker.task import Task

    assert isinstance(task, Task)
    try:
        layout = measure(sandbox, task, body, types)
    except RuntimeError:  # the match is already recorded; its types just go unmeasured
        return 0
    rows = 0
    with conn:
        for t in types:
            entry = layout[t.name]
            assert isinstance(entry, dict)
            conn.execute(
                "INSERT OR REPLACE INTO provisional_types (function_id, attempt_id, name, kind,"
                " size, fields_json, signature, source, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fn_id,
                    attempt_id,
                    t.name,
                    t.kind,
                    entry["size"],
                    json.dumps(entry["fields"]),
                    signature(entry),
                    t.source,
                    now(),
                ),
            )
            rows += 1
    return rows


def run_attempt(s: Session, spec: AttemptSpec, log: Log) -> tuple[int, Classified]:
    """Run one attempt; returns (attempt id, classification). Uses its own DB connection."""
    conn = store.connect(s.db_path)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO attempts (run_id, function_id, task_kind, model, effort, escalation,"
                " prompt_version, context_version, started_at) VALUES (?, ?, 'function', ?, ?, ?,"
                " ?, ?, ?)",
                (
                    spec.run_id,
                    spec.fn.id,
                    spec.model,
                    spec.effort,
                    spec.escalation,
                    prompts.prompt_version(),
                    CONTEXT_VERSION,
                    now(),
                ),
            )
        assert cur.lastrowid is not None
        attempt_id = cur.lastrowid
        sandbox = s.settings.rematch_workdir.expanduser() / "attempts" / str(attempt_id)
        run_dir = s.settings.rematch_var / "runs" / str(spec.run_id) / str(attempt_id)
        control = run_dir / "control"  # outside the sandbox, under the read-denied REMATCH_VAR
        task = build_sandbox(s, spec.fn, sandbox, spec.options, control)
        manifest = seal(sandbox)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")

        cfg = InvokeConfig(
            claude_bin=spec.claude_bin or s.settings.claude_bin,
            model=spec.model,
            effort=spec.effort,
            auth_mode=s.settings.worker_auth_mode,
            max_turns=s.settings.max_turns,
            timeout_sec=s.settings.attempt_timeout_sec,
            max_budget_usd=s.settings.max_budget_usd_per_attempt,
            system_prompt_file=spec.system_prompt,
            deny_read_roots=deny_roots(s),
            api_key=s.settings.anthropic_api_key.get_secret_value(),
        )
        stream = s.settings.rematch_var / "logs" / str(spec.run_id) / f"{attempt_id}.jsonl"
        result, totals = _run_segments(cfg, sandbox, stream, spec, log)
        c = classify(sandbox, manifest, result, task, control)

        best_path = None
        if c.best_check is not None:
            best_path = str(sandbox / ".history" / f"{c.best_check:03d}.c")
        with conn:
            conn.execute(
                "UPDATE attempts SET ended_at = ?, outcome = ?, checks_used = ?, best_score = ?,"
                " final_score = ?, num_turns = ?, input_tokens = ?, output_tokens = ?,"
                " cache_read_tokens = ?, cost_usd = ?, sandbox_path = ?, transcript_path = ?,"
                " best_candidate_path = ? WHERE id = ?",
                (
                    now(),
                    c.outcome,
                    c.checks_used,
                    c.best_score,
                    c.final_score,
                    totals.turns,
                    totals.input_tokens,
                    totals.output_tokens,
                    totals.cache_read_tokens,
                    totals.cost_usd,
                    str(sandbox),
                    str(stream),
                    best_path,
                    attempt_id,
                ),
            )
            for note in note_bullets(sandbox):
                conn.execute(
                    "INSERT INTO learning_candidates (attempt_id, text, created_at)"
                    " VALUES (?, ?, ?)",
                    (attempt_id, note, now()),
                )
            if spec.kind != "bench":
                status = "matched" if c.outcome == "match" else "failed"
                conn.execute(
                    "UPDATE functions SET status = ?, attempts = attempts + 1, best_score ="
                    " max(coalesce(best_score, 0), ?), updated_at = ? WHERE id = ?",
                    (status, c.best_score or 0.0, now(), spec.fn.id),
                )
            if spec.kind != "bench" and c.outcome == "match" and c.best_source is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO matches (function_id, attempt_id, c_source,"
                    " verified_versions, lint_json, verified_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        spec.fn.id,
                        attempt_id,
                        c.best_source,
                        ",".join(c.verified_versions),
                        lint(c.best_source).to_json(),
                        now(),
                    ),
                )
        if spec.kind != "bench" and c.outcome == "match" and c.best_source is not None:
            n = _record_types(conn, sandbox, task, spec.fn.id, attempt_id, c.best_source)
            if n:
                log(f"attempt {attempt_id}: stored {n} provisional type(s)")
        log(
            f"attempt {attempt_id} {spec.fn.name}: {c.outcome} (best {c.best_score}, "
            f"{c.checks_used} checks, {totals.turns} turns, {totals.seconds:.0f}s"
            + (f", resumed {totals.segments - 1}x" if totals.segments > 1 else "")
            + ")"
        )
        prune_sandboxes(s, conn, spec.fn.id)
        return attempt_id, c
    finally:
        conn.close()


def prune_sandboxes(s: Session, conn: sqlite3.Connection, fn_id: int) -> int:
    """Delete a function's finished attempt sandboxes that nothing will read again (SPEC §7.1):
    all of them once it matched (the source is in `matches`), else all but the latest and the
    best-scoring one (the next rung's previous_best.c). Each holds a megabyte-sized ctx.h."""
    rows = conn.execute(
        "SELECT a.id, a.sandbox_path, coalesce(a.best_score, -1) FROM attempts a"
        " WHERE a.function_id = ? AND a.task_kind = 'function' AND a.ended_at IS NOT NULL"
        " AND a.sandbox_path IS NOT NULL ORDER BY a.id",
        (fn_id,),
    ).fetchall()
    matched = conn.execute("SELECT 1 FROM matches WHERE function_id = ?", (fn_id,)).fetchone()
    keep: set[int] = set()
    if not matched and rows:
        keep = {rows[-1][0], max(rows, key=lambda r: (r[2], r[0]))[0]}
    root = (s.settings.rematch_workdir.expanduser() / "attempts").resolve()
    removed = 0
    for aid, path, _ in rows:
        sandbox = Path(path)
        if aid in keep or not sandbox.is_dir() or sandbox.resolve().parent != root:
            continue
        shutil.rmtree(sandbox, ignore_errors=True)
        removed += 1
    return removed
