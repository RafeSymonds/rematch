"""`tu_fix` (SPEC §10 step 3, Phase 4a): an agent repairs a C piece that fails the piece check.

The sandbox holds candidate.c (the composed piece), each function's assembly in targets/, a
flattened ctx.h to grep, and ./check, which runs `rematch tu-check` against the control
directory (piece.json and target caches, outside the sandbox). A source that passes is stored
in piece_sources; the integrator uses it for a piece with exactly these functions.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rematch import prompts
from rematch.context import CONTEXT_VERSION
from rematch.integrate.piece import PieceResult, PieceSpec, check_source
from rematch.session import Session
from rematch.worker.attempt import (
    AttemptSpec,
    _run_segments,
    deny_roots,
    finish_run,
    now,
    run_config,
    start_run,
)
from rematch.worker.invoke import InvokeConfig
from rematch.worker.sandbox import FunctionRecord, make_context
from rematch.worker.task import CompileSpec

Log = Callable[[str], None]
STATE = "tu_state.json"


# --- ./check in the sandbox (no Session: control files only) -----------------------------


def tu_check(sandbox: Path, control: Path) -> tuple[str, int]:
    spec = PieceSpec.load(control)
    state_path = control / STATE
    state = json.loads(state_path.read_text()) if state_path.is_file() else {"checks": 0}
    if state.get("matched"):
        return "MATCH already recorded. Write SUMMARY.json and end.", 0
    if state["checks"] >= spec.max_checks:
        return f"STOP: check limit ({spec.max_checks}) reached. Write SUMMARY.json and end.", 1
    state["checks"] += 1
    source = (sandbox / "candidate.c").read_text(encoding="utf-8", errors="replace")
    history = control / "history"
    history.mkdir(parents=True, exist_ok=True)
    (history / f"{state['checks']:03d}.c").write_text(source, encoding="utf-8")
    result = check_source(spec, control, source, control / "build")
    best = state.get("best_matched", -1)
    if result.matched > best and not result.errors:
        state["best_matched"] = result.matched
        state["best_check"] = state["checks"]
    if result.ok:
        state["matched"] = True
        state["best_check"] = state["checks"]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    head = (
        f"check {state['checks']}/{spec.max_checks} | "
        f"{result.matched}/{len(spec.functions)} functions match"
    )
    if result.ok:
        return f"MATCH ({head})", 0
    return head + "\n" + result.report(), 1


# --- running an attempt ------------------------------------------------------------------


@dataclass
class FixOutcome:
    attempt_id: int
    matched: bool
    checks: int
    cost: float


def _task_md(spec: PieceSpec, first: PieceResult) -> str:
    return (
        f"# Task: make candidate.c compile with every function matching ({spec.tu})\n\n"
        f"Functions, in order: {', '.join(spec.functions)}. Their assembly is in targets/.\n"
        "ctx.h is the flattened content of candidate.c's headers: grep it for declarations;"
        " never read it whole.\n\n"
        f"## First ./check\n\n```\n{first.report()}\n```\n\n"
        f"Budget: at most {spec.max_checks} runs of ./check.\n"
    )


def run_tu_fix(
    s: Session,
    conn: sqlite3.Connection,
    control_src: Path,
    source: str,
    targets_asm: dict[str, str],
    model: str,
    effort: str,
    log: Log,
) -> FixOutcome:
    spec = PieceSpec.load(control_src)
    run_id = start_run(conn, "live", run_config(s, model, effort, {"tu_fix": spec.tu}), None)
    with conn:
        cur = conn.execute(
            "INSERT INTO attempts (run_id, function_id, task_kind, model, effort, escalation,"
            " prompt_version, context_version, started_at) VALUES (?, NULL, 'tu_fix', ?, ?, 0,"
            " ?, ?, ?)",
            (run_id, model, effort, 1, CONTEXT_VERSION, now()),
        )
    attempt_id = int(cur.lastrowid or 0)
    sandbox = s.settings.rematch_workdir.expanduser() / "tu_fix" / str(attempt_id)
    control = s.settings.rematch_var / "runs" / str(run_id) / str(attempt_id) / "control"
    shutil.rmtree(control, ignore_errors=True)
    shutil.copytree(control_src, control, ignore=shutil.ignore_patterns("build", "history", STATE))
    sandbox.mkdir(parents=True)
    (sandbox / "candidate.c").write_text(source, encoding="utf-8")
    (sandbox / "targets").mkdir()
    for name, text in targets_asm.items():
        (sandbox / "targets" / f"{name}.s").write_text(text + "\n", encoding="utf-8")
    first_version = next(iter(spec.compile))
    entry = CompileSpec.model_validate(spec.compile[first_version]).entry()
    stub = sandbox / ".ctx_stub.c"
    stub.write_text(
        "".join(ln + "\n" for ln in source.splitlines() if ln.startswith("#include")),
        encoding="utf-8",
    )
    (sandbox / "ctx.h").write_text(make_context(s, first_version, stub, entry), encoding="utf-8")
    stub.unlink()
    first = check_source(spec, control, source, control / "build")
    (sandbox / "TASK.md").write_text(_task_md(spec, first), encoding="utf-8")
    (sandbox / "NOTES.md").write_text("", encoding="utf-8")
    (sandbox / "SUMMARY.json").write_text("{}\n", encoding="utf-8")
    rematch = Path(sys.executable).parent / "rematch"
    check = sandbox / "check"
    check.write_text(
        f'#!/bin/sh\nexec "{rematch}" tu-check --sandbox "{sandbox}" --control "{control}"\n',
        encoding="utf-8",
    )
    check.chmod(0o755)
    system = control.parent / "system.md"
    system.write_text(
        prompts.render("tu_fix.md", target_intro=s.definition.worker.intro), encoding="utf-8"
    )
    cfg = InvokeConfig(
        claude_bin=s.settings.claude_bin,
        model=model,
        effort=effort,
        auth_mode=s.settings.worker_auth_mode,
        max_turns=s.settings.max_turns,
        timeout_sec=s.settings.attempt_timeout_sec,
        max_budget_usd=s.settings.max_budget_usd_per_attempt,
        system_prompt_file=system,
        deny_read_roots=deny_roots(s),
        api_key=s.settings.anthropic_api_key.get_secret_value(),
    )
    stream = s.settings.rematch_var / "logs" / str(run_id) / f"{attempt_id}.jsonl"
    fake = FunctionRecord(0, spec.tu, 0, "", spec.tu, "asm", "thumb", False)
    attempt = AttemptSpec(run_id, "live", fake, None, model, effort, system)  # type: ignore[arg-type]
    _, totals = _run_segments(cfg, sandbox, stream, attempt, log)
    state = json.loads((control / STATE).read_text()) if (control / STATE).is_file() else {}
    matched = bool(state.get("matched"))
    best = state.get("best_check")
    with conn:
        conn.execute(
            "UPDATE attempts SET ended_at = ?, outcome = ?, checks_used = ?, num_turns = ?,"
            " input_tokens = ?, output_tokens = ?, cache_read_tokens = ?, cost_usd = ?,"
            " sandbox_path = ?, transcript_path = ?, best_candidate_path = ? WHERE id = ?",
            (
                now(),
                "match" if matched else "nonmatch",
                state.get("checks", 0),
                totals.turns,
                totals.input_tokens,
                totals.output_tokens,
                totals.cache_read_tokens,
                totals.cost_usd,
                str(sandbox),
                str(stream),
                str(control / "history" / f"{best:03d}.c") if best else None,
                attempt_id,
            ),
        )
        if matched and best:
            fixed = (control / "history" / f"{best:03d}.c").read_text(encoding="utf-8")
            conn.execute(
                "INSERT INTO piece_sources (tu_path, functions_json, source, attempt_id,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (spec.tu, json.dumps(spec.functions), fixed, attempt_id, now()),
            )
    finish_run(conn, run_id, notes=f"tu_fix {spec.tu}")
    log(f"tu_fix {spec.tu}: {'match' if matched else 'no fix'} ({state.get('checks', 0)} checks)")
    return FixOutcome(attempt_id, matched, int(state.get("checks", 0)), totals.cost_usd)
