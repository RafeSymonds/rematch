"""One attempt end to end: sandbox, seal, worker, classification, records (SPEC §7, §5)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rematch import prompts
from rematch.context import CONTEXT_VERSION
from rematch.db import store
from rematch.session import Session
from rematch.types.probe import measure, signature
from rematch.types.provisional import TypesError, extract_block, parse_block
from rematch.worker.invoke import InvokeConfig, run_worker
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
    layout = measure(sandbox, task, body, types)
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
                " prompt_version, context_version, started_at) VALUES (?, ?, 'function', ?, ?, 0,"
                " ?, ?, ?)",
                (
                    spec.run_id,
                    spec.fn.id,
                    spec.model,
                    spec.effort,
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
        prompt = (sandbox / "TASK.md").read_text(encoding="utf-8")
        result = run_worker(cfg, sandbox, prompt, stream)
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
                    result.num_turns,
                    result.usage("input_tokens"),
                    result.usage("output_tokens"),
                    result.usage("cache_read_input_tokens"),
                    result.cost_usd,
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
                        json.dumps({"phase2": "pre-filter only; §9 lints arrive in Phase 4"}),
                        now(),
                    ),
                )
        if spec.kind != "bench" and c.outcome == "match" and c.best_source is not None:
            n = _record_types(conn, sandbox, task, spec.fn.id, attempt_id, c.best_source)
            if n:
                log(f"attempt {attempt_id}: stored {n} provisional type(s)")
        log(
            f"attempt {attempt_id} {spec.fn.name}: {c.outcome} (best {c.best_score}, "
            f"{c.checks_used} checks, {result.num_turns} turns, {result.seconds:.0f}s)"
        )
        return attempt_id, c
    finally:
        conn.close()
