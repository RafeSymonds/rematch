"""`tu_data` (SPEC §10, Phase 4b): an agent moves a fully converted TU's data into its C file.

The sandbox holds candidate.c (the TU's integrated C file), data.s (the assembly piece that
still holds the data), each function's assembly in targets/, a grep-only ctx.h, and ./check,
which runs `rematch data-check` against the control directory (data.json, outside the
sandbox). A passing source is stored in piece_sources with kind 'data'; the integrator then
replaces the assembly piece and the C file with that one file.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

from rematch import prompts
from rematch.context import CONTEXT_VERSION
from rematch.integrate.data_check import DataSpec, check_data, prepare_data
from rematch.integrate.tu_fix import FixOutcome, Log
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

STATE = "data_state.json"
PROMPT_VERSION = 1


def data_check(sandbox: Path, control: Path) -> tuple[str, int]:
    """./check in a tu_data sandbox."""
    spec = DataSpec.load(control)
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
    result = check_data(spec, source, control / "build")
    if result.ok:
        state["matched"] = True
        state["best_check"] = state["checks"]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    head = f"check {state['checks']}/{spec.max_checks}"
    if result.ok:
        return f"MATCH ({head}): data and every function match", 0
    return head + "\n" + result.report(), 1


def _task_md(tu: str, report: str, max_checks: int) -> str:
    return (
        f"# Task: define the data of {tu} in candidate.c\n\n"
        "data.s is the assembly that still holds this TU's data. candidate.c already holds"
        " every function, and they match. Define the data in candidate.c so that the object's"
        " data sections are byte-identical to data.s, and every function still matches."
        " targets/ has each function's assembly. ctx.h is the flattened content of"
        " candidate.c's headers: grep it for declarations; never read it whole.\n\n"
        f"## First ./check\n\n```\n{report}\n```\n\n"
        f"Budget: at most {max_checks} runs of ./check.\n"
    )


def data_piece_text(live: Path, pieces: list[str]) -> tuple[str, str] | None:
    """(asm piece path, C piece path) when the TU is exactly one data-only assembly piece and
    one C file, else None."""
    asm = [p for p in pieces if p.endswith(".s")]
    c = [p for p in pieces if p.endswith(".c")]
    if len(asm) != 1 or len(c) != 1 or not (live / asm[0]).is_file():
        return None
    return asm[0], c[0]


def run_tu_data(
    s: Session,
    conn: sqlite3.Connection,
    tu: str,
    functions: list[str],
    asm_piece: str,
    c_piece: str,
    targets_asm: dict[str, str],
    model: str,
    effort: str,
    log: Log,
) -> FixOutcome:
    from rematch.integrate.run import stored_source

    live = s.settings.live_worktree
    assert live is not None
    # start from the types step's source when it has one, so the data lands on merged types
    source = stored_source(conn, tu, functions, "types") or (live / c_piece).read_text("utf-8")
    run_id = start_run(conn, "live", run_config(s, model, effort, {"tu_data": tu}), None)
    with conn:
        cur = conn.execute(
            "INSERT INTO attempts (run_id, function_id, task_kind, model, effort, escalation,"
            " prompt_version, context_version, started_at) VALUES (?, NULL, 'tu_data', ?, ?, 0,"
            " ?, ?, ?)",
            (run_id, model, effort, PROMPT_VERSION, CONTEXT_VERSION, now()),
        )
    attempt_id = int(cur.lastrowid or 0)
    sandbox = s.settings.rematch_workdir.expanduser() / "tu_data" / str(attempt_id)
    control = s.settings.rematch_var / "runs" / str(run_id) / str(attempt_id) / "control"
    shutil.rmtree(control, ignore_errors=True)
    spec = prepare_data(s, tu, functions, control)
    sandbox.mkdir(parents=True)
    (sandbox / "candidate.c").write_text(source, encoding="utf-8")
    (sandbox / "data.s").write_text((live / asm_piece).read_text(encoding="utf-8"), "utf-8")
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
    first = check_data(spec, source, control / "build")
    (sandbox / "TASK.md").write_text(_task_md(tu, first.report(), spec.max_checks), "utf-8")
    (sandbox / "NOTES.md").write_text("", encoding="utf-8")
    (sandbox / "SUMMARY.json").write_text("{}\n", encoding="utf-8")
    rematch = Path(sys.executable).parent / "rematch"
    check = sandbox / "check"
    check.write_text(
        f'#!/bin/sh\nexec "{rematch}" data-check --sandbox "{sandbox}" --control "{control}"\n',
        encoding="utf-8",
    )
    check.chmod(0o755)
    system = control.parent / "system.md"
    system.write_text(
        prompts.render("tu_data.md", target_intro=s.definition.worker.intro), encoding="utf-8"
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
    fake = FunctionRecord(0, tu, 0, "", tu, "asm", "thumb", False)
    attempt = AttemptSpec(run_id, "live", fake, None, model, effort, system)  # type: ignore[arg-type]
    _, totals = _run_segments(cfg, sandbox, stream, attempt, log)
    state = json.loads((control / STATE).read_text()) if (control / STATE).is_file() else {}
    matched = bool(state.get("matched"))
    best = state.get("best_check")
    best_path = control / "history" / f"{best:03d}.c" if best else None
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
                str(best_path) if best_path else None,
                attempt_id,
            ),
        )
        if matched and best_path is not None:
            conn.execute(
                "INSERT INTO piece_sources (tu_path, functions_json, source, attempt_id,"
                " created_at, kind) VALUES (?, ?, ?, ?, ?, 'data')",
                (tu, json.dumps(functions), best_path.read_text("utf-8"), attempt_id, now()),
            )
    finish_run(conn, run_id, notes=f"tu_data {tu}")
    log(f"tu_data {tu}: {'match' if matched else 'no match'} ({state.get('checks', 0)} checks)")
    return FixOutcome(attempt_id, matched, int(state.get("checks", 0)), totals.cost_usd)
