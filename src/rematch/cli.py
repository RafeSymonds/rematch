"""`rematch` command-line entry point (SPEC §13)."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

import typer
from pydantic import ValidationError

from rematch.config import Settings
from rematch.db import store
from rematch.doctor import iter_doctor
from rematch.session import Session, SessionError, open_session
from rematch.target.definition import TargetDefError

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _echo_err(msg: str) -> None:
    typer.echo(f"  .. {msg}", err=True)


def _settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # required keys come from the environment
    except ValidationError as exc:
        typer.echo(f"FAIL config: {exc}")
        raise typer.Exit(1) from exc


def _session() -> Session:
    try:
        return open_session(_settings())
    except SessionError as exc:
        typer.echo(f"FAIL {exc}")
        raise typer.Exit(1) from exc


@app.callback()
def main() -> None:
    """Agent harness for matching decompilation."""


@app.command()
def doctor() -> None:
    """Check Claude Code, workdir isolation, push guard, vanilla builds, hashes and the DB."""
    settings = _settings()
    failed = False
    for result in iter_doctor(settings, log=_echo_err):
        typer.echo(f"{'PASS' if result.ok else 'FAIL'} {result.name}: {result.detail}")
        failed |= not result.ok
    if failed:
        raise typer.Exit(1)


@app.command()
def inventory(
    history: bool = typer.Option(
        False, "--history", help="Compute decompiled_at for human C (needs a prior inventory)."
    ),
) -> None:
    """Parse the vanilla build: every linked function, its features and difficulty, into the DB."""
    from rematch.inventory.run import run_inventory

    s = _session()
    if history:
        _inventory_history(s)
        return
    conn = store.connect(s.db_path)
    try:
        report, log_path = run_inventory(
            s.definition,
            s.worktree,
            s.primary_version,
            conn,
            s.settings.objdump,
            s.settings.ctags,
            s.settings.rematch_var / "logs",
            _echo_err,
            s.header_index_path,
        )
    except TargetDefError as exc:
        typer.echo(f"FAIL target.toml: {exc}")
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    typer.echo(f"TUs: {report.tus}")
    for kind, count in sorted(report.functions.items()):
        typer.echo(f"functions {kind}: {count}")
    typer.echo(f"skipped (excluded): {report.skipped}")
    typer.echo(f"unlinked (in object, not in map): {len(report.unlinked)}")
    typer.echo(f"asm address mismatches: {len(report.address_mismatches)}")
    typer.echo(f"TUs without an object: {len(report.missing_objects)}")
    unlinked_files = report.unlinked_asm_files
    typer.echo(
        f"asm files not in the link order: {len(unlinked_files)} "
        f"({sum(unlinked_files.values())} functions, not inventoried)"
    )
    typer.echo(f"log: {log_path}")


@app.command()
def cd(
    sandbox: Path = typer.Option(Path("."), help="Sandbox directory."),
    json_out: Path | None = typer.Option(None, help="Also write the result JSON here."),
    control: Path | None = typer.Option(None, help="Control dir (task.json); default: sandbox."),
    mode: str = typer.Option("isolated", help="Compile mode (only isolated in Phase 1)."),
) -> None:
    """Compile candidate.c and diff it against the target (what ./check runs)."""
    from rematch.worker.check_entry import run_check

    if mode != "isolated":
        typer.echo(f"mode {mode!r} is not available yet")
        raise typer.Exit(2)
    text, code, _ = run_check(sandbox.resolve(), json_out, control.resolve() if control else None)
    typer.echo(text)
    raise typer.Exit(code)


@app.command()
def ctx(
    function: str = typer.Argument(..., help="Name, 0xADDR or module:0xADDR."),
    out: Path = typer.Option(..., help="Directory to create."),
    with_source: bool = typer.Option(
        False, help="Seed candidate.c with the human C (c_human only; never for bench)."
    ),
    bench: bool = typer.Option(False, help="Bench presentation: anonymous name, scrubbed ctx."),
) -> None:
    """Build a sandbox for one function without running a worker."""
    from rematch.worker.sandbox import SandboxError, SandboxOptions, build_sandbox, find_function

    s = _session()
    conn = store.connect(s.db_path)
    try:
        task = build_sandbox(
            s,
            find_function(conn, function),
            out,
            SandboxOptions(with_source=with_source, bench=bench),
        )
    except SandboxError as exc:
        typer.echo(f"FAIL {exc}")
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    typer.echo(f"{out}: {task.symbol} ({task.mode}), versions {[v.version for v in task.versions]}")


@app.command()
def roundtrip(
    n: int = typer.Option(50, help="Random human-C functions to round-trip."),
    seed: int = typer.Option(42),
    golden: bool = typer.Option(False, help="Run the target's golden set and mutants instead."),
) -> None:
    """Compile human C from its own source through ./check; it must match (Phase 1 exit)."""
    from rematch.worker.roundtrip import (
        load_golden,
        run_mutants,
        sample_c_functions,
        summarize,
    )
    from rematch.worker.roundtrip import (
        roundtrip as run,
    )
    from rematch.worker.sandbox import SandboxError, find_function

    s = _session()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    root = s.settings.rematch_workdir.expanduser() / "roundtrip" / stamp
    conn = store.connect(s.db_path)
    try:
        if golden:
            spec = load_golden(s)
            outcomes = run(s, [find_function(conn, f) for f in spec.functions], root, _echo_err)
            outcomes += run_mutants(s, conn, spec.mutants, root, _echo_err)
        else:
            outcomes = run(s, sample_c_functions(conn, n, seed), root, _echo_err)
    except SandboxError as exc:
        typer.echo(f"FAIL {exc}")
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    summary = summarize(outcomes)
    log_dir = s.settings.rematch_var / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"roundtrip-{stamp}.json"
    log_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    for o in outcomes:
        if not o.ok:
            typer.echo(f"MISS {o.function}: {o.status} {o.detail[:3]} hints={o.hints}")
    typer.echo(
        f"ok {summary['ok']}/{summary['total']} | by status {summary['by_status']} | "
        f"median ./check {summary['median_check_seconds']} s"
    )
    typer.echo(f"sandboxes: {root}")
    typer.echo(f"log: {log_path}")
    if summary["ok"] != summary["total"]:
        raise typer.Exit(1)


def _inventory_history(s: Session) -> None:
    from rematch.inventory.history import HistoryError, run_history, targets_from_db

    order = s.definition.require(s.definition.link_order, "link_order")
    asm = s.definition.require(s.definition.asm, "asm")
    conn = store.connect(s.db_path)
    try:
        targets, unresolved = targets_from_db(s.definition, s.worktree, conn, s.primary_version)
        dates, report = run_history(
            s.settings.target_repo,
            s.settings.target_pin,
            s.settings.bench_cutoff_date,
            [order.file, *order.history],
            asm.format,
            targets,
            _echo_err,
        )
        with conn:
            conn.execute("UPDATE functions SET decompiled_at = NULL WHERE source_kind = 'c_human'")
            conn.executemany(
                "UPDATE functions SET decompiled_at = ? WHERE id = ?",
                [(c.date, fid) for fid, c in dates.items()],
            )
    except HistoryError as exc:
        typer.echo(f"FAIL history: {exc}")
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    report.no_address = unresolved
    log_dir = s.settings.rematch_var / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = log_dir / f"history-{stamp}.json"
    path.write_text(json.dumps(dataclasses.asdict(report), indent=2), encoding="utf-8")
    typer.echo(f"cutoff commit {report.cutoff_commit[:12]} ({report.cutoff_date})")
    typer.echo(f"human-C functions: {report.c_functions} (+{unresolved} without an address)")
    typer.echo(f"decompiled after the cutoff (clean pool before pre-screen): {report.dated}")
    typer.echo(f"commits read: {report.commits_read} of {report.commits_after}")
    typer.echo(f"log: {path}")


def _confirm_spend(what: str, attempts: int, yes: bool) -> None:
    """Token-spending commands print an estimate and need --yes or a TTY confirmation (§13)."""
    import sys

    s = _settings()
    typer.echo(
        f"{what}: {attempts} worker attempt(s), each up to {s.max_turns} turns, "
        f"{s.max_checks} checks and {s.attempt_timeout_sec:g} s ({s.worker_auth_mode} auth)."
    )
    if yes:
        return
    if not sys.stdin.isatty() or not typer.confirm("This spends model tokens. Continue?"):
        typer.echo("Not confirmed. Pass --yes to run.")
        raise typer.Exit(2)


def _model_effort(model: str | None, effort: str | None) -> tuple[str, str]:
    s = _settings()
    return model or s.worker_model, effort or s.worker_effort


@app.command()
def selftest(
    n: int = typer.Option(10, help="Functions for the oracle run."),
    workers: int = typer.Option(4, help="Parallel attempts."),
) -> None:
    """Oracle + saboteur fake agents through the full pipeline; zero model calls (§14)."""
    from rematch.selftest.run import run_selftest

    s = _session()
    checks = run_selftest(s, n, workers, _echo_err)
    for c in checks:
        typer.echo(f"{'PASS' if c.ok else 'FAIL'} {c.name}: {c.detail}")
    if not all(c.ok for c in checks):
        raise typer.Exit(1)


@app.command()
def bench(
    n: int = typer.Option(..., help="Set size."),
    seed: int = typer.Option(42),
    pool: str = typer.Option("clean", help="clean or contaminated (§11.1)."),
    size_bucket: str | None = typer.Option(
        None, "--bucket", help="Only one size bucket: S, M or L (set id gets -S/-M/-L)."
    ),
    workers: int | None = typer.Option(None, help="Parallel attempts (default WORKER_COUNT)."),
    model: str | None = typer.Option(None),
    effort: str | None = typer.Option(None),
    yes: bool = typer.Option(False, "--yes", help="Confirm token spend."),
) -> None:
    """Run a frozen bench set with real workers (spends tokens)."""
    from rematch.bench.pool import Prescreen, PrescreenError, pool_sizes
    from rematch.bench.report import append_row, benchmarks_row, format_report, run_metrics
    from rematch.bench.run import run_functions
    from rematch.bench.sets import bucket, load_set, save_set, set_id, stratified_sample
    from rematch.target.definition import target_dir
    from rematch.worker.sandbox import find_function

    if pool not in ("clean", "contaminated"):
        typer.echo("--pool must be clean or contaminated")
        raise typer.Exit(2)
    s = _session()
    cutoff = s.settings.bench_cutoff_date
    if size_bucket not in (None, "S", "M", "L"):
        typer.echo("--bucket must be S, M or L")
        raise typer.Exit(2)
    sid = set_id(n, seed, cutoff) + ("" if pool == "clean" else "-contaminated")
    sid += f"-{size_bucket}" if size_bucket else ""
    conn = store.connect(s.db_path)
    try:
        ids = load_set(conn, sid)
        if ids is None:
            sizes = pool_sizes(conn, pool, cutoff)
            if size_bucket:
                sizes = {k: v for k, v in sizes.items() if bucket(v) == size_bucket}
            typer.echo(f"pool {pool}: {len(sizes)} functions; building set {sid}")
            prescreen = Prescreen(s, conn, _echo_err, s.settings.worker_count)
            try:
                ids, rejected = stratified_sample(sizes, n, seed, prescreen.eligible)
            except PrescreenError as exc:
                typer.echo(f"FAIL {exc}; set not saved")
                raise typer.Exit(1) from exc
            if len(ids) < min(n, len(sizes)) // 2:
                typer.echo(f"FAIL only {len(ids)} eligible functions; set not saved")
                raise typer.Exit(1)
            save_set(conn, sid, ids, seed, cutoff)
            typer.echo(f"set {sid}: {len(ids)} functions ({len(rejected)} failed the pre-screen)")
        fns = []
        for fid in ids:
            module, address = conn.execute(
                "SELECT module, address FROM functions WHERE id = ?", (fid,)
            ).fetchone()
            fns.append(find_function(conn, f"{module}:{address:#x}"))
    finally:
        conn.close()
    m, e = _model_effort(model, effort)
    effort_for = {} if effort else _effort_by_size(s, [fn.id for fn in fns])
    _confirm_spend(f"bench {sid}", len(fns), yes)
    result = run_functions(
        s,
        "bench",
        fns,
        model=m,
        effort="by-size" if effort_for else e,
        workers=workers or s.settings.worker_count,
        bench=True,
        seed=seed,
        log=_echo_err,
        bench_set=sid,
        effort_for=effort_for,
        extra_config={"effort_by_size": s.settings.effort_by_size if effort_for else None},
    )
    conn = store.connect(s.db_path)
    try:
        metrics = run_metrics(conn, result.run_id)
    finally:
        conn.close()
    typer.echo(format_report(metrics))
    row = benchmarks_row(metrics, s.definition.name, "baseline" if seed == 42 else "")
    path = append_row(target_dir(s.settings.target_repo), row)
    typer.echo(f"row appended to {path}")


@app.command()
def attempt(
    function: str = typer.Argument(..., help="Name, 0xADDR or module:0xADDR."),
    model: str | None = typer.Option(None),
    effort: str | None = typer.Option(None),
    yes: bool = typer.Option(False, "--yes", help="Confirm token spend."),
) -> None:
    """One live attempt on one function (spends tokens)."""
    from rematch.bench.run import run_functions
    from rematch.worker.sandbox import find_function

    s = _session()
    conn = store.connect(s.db_path)
    try:
        fn = find_function(conn, function)
    finally:
        conn.close()
    m, e = _model_effort(model, effort)
    _confirm_spend(f"attempt {fn.name}", 1, yes)
    result = run_functions(
        s, "live", [fn], model=m, effort=e, workers=1, bench=False, seed=0, log=_echo_err
    )
    attempt_id, _, c = result.attempts[0]
    typer.echo(f"attempt {attempt_id}: {c.outcome} (best {c.best_score})")


@app.command()
def report(
    run: int | None = typer.Option(None, help="Run id (default: the last run)."),
    compare: int | None = typer.Option(None, help="Another run to show next to it."),
) -> None:
    """Metrics for a run (§11.3)."""
    from rematch.bench.report import format_report, run_metrics

    s = _session()
    conn = store.connect(s.db_path)
    try:
        run_id = run
        if run_id is None:
            row = conn.execute("SELECT max(id) FROM runs").fetchone()
            run_id = row[0] if row else None
        if run_id is None:
            typer.echo("no runs yet")
            raise typer.Exit(1)
        typer.echo(format_report(run_metrics(conn, run_id)))
        if compare is not None:
            typer.echo(format_report(run_metrics(conn, compare)))
    finally:
        conn.close()


def _effort_by_size(s: Session, ids: list[int]) -> dict[int, str]:
    """EFFORT_BY_SIZE applied per function (empty setting: no per-size effort)."""
    from rematch.bench.sets import bucket

    if not s.settings.effort_by_size:
        return {}
    conn = store.connect(s.db_path)
    try:
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, n_insns FROM functions WHERE id IN ({marks})", ids
        ).fetchall()
    finally:
        conn.close()
    return {int(fid): s.settings.effort_for_size(bucket(int(n or 0))) for fid, n in rows}


learnings_app = typer.Typer(no_args_is_help=True, help="Curate worker-proposed learnings (D9).")
app.add_typer(learnings_app, name="learnings")


@learnings_app.command("review")
def learnings_review(
    list_only: bool = typer.Option(False, "--list", help="Print pending candidates and exit."),
    accept: list[int] = typer.Option([], "--accept", help="Candidate ids to accept."),
    to: str = typer.Option("compiler", "--to", help="compiler (rematch) or target (.rematch/)."),
    reject: list[int] = typer.Option([], "--reject", help="Candidate ids to reject."),
) -> None:
    """Accept or reject learning candidates by hand; accepted ones become rules (no evidence)."""
    import sys

    from rematch.learnings.curate import decide, pending
    from rematch.learnings.curate_run import rule_files
    from rematch.learnings.rules import MAX_LINES, Rule

    s = _session()
    comp, tgt = rule_files(s)
    files = {"compiler": comp, "target": tgt}
    if to not in files:
        typer.echo("--to must be compiler or target")
        raise typer.Exit(2)
    conn = store.connect(s.db_path)
    try:
        todo = dict(pending(conn))
        if list_only:
            for cid, text in todo.items():
                typer.echo(f"{cid}: {text}")
            typer.echo(f"{len(todo)} pending")
            return
        decisions: list[tuple[int, str | None]] = [(c, to) for c in accept] + [
            (c, None) for c in reject
        ]
        if not decisions and sys.stdin.isatty():
            for cid, text in todo.items():
                typer.echo(f"\n[{cid}] {text}")
                choice = typer.prompt("c=compiler t=target r=reject s=skip q=quit", default="s")
                if choice == "q":
                    break
                if choice in ("c", "t", "r"):
                    decisions.append((cid, {"c": "compiler", "t": "target"}.get(choice)))
        for cid, dest in decisions:
            if cid not in todo:
                typer.echo(f"{cid}: not pending; skipped")
                continue
            if dest is not None:
                f = files[dest]
                text = " ".join(todo[cid].split())
                if any(r.text == text for r in f.rules):
                    typer.echo(f"{cid}: already in {f.path.name}")
                else:
                    rid = f.next_id("T" if dest == "target" else "C")
                    f.rules.append(Rule(rid, text, None, [cid]))  # no evidence: unverified
                    typer.echo(f"{cid}: added as {rid} to {f.path}")
            decide(conn, cid, accept=dest is not None)
    finally:
        conn.close()
    for name, f in files.items():
        f.save()
        if len(f.rules) > MAX_LINES:
            typer.echo(f"warning: {name} has {len(f.rules)} rules (cap {MAX_LINES})")


@app.command("try")
def try_cmd(
    a: Path = typer.Argument(..., help="First C snippet."),
    b: Path = typer.Argument(..., help="Second C snippet."),
    control: Path = typer.Option(..., help="Control dir with trial.json."),
) -> None:
    """Compile two snippets with the target's compiler and show how their code differs."""
    from rematch.learnings.trial import Compiler, run_trial
    from rematch.toolchain.runner import ToolchainRunner
    from rematch.worker.task import CompileSpec

    cfg = json.loads((control / "trial.json").read_text(encoding="utf-8"))
    compiler = Compiler(
        CompileSpec(**cfg["compile"]).entry(),
        ToolchainRunner.from_spec(cfg["toolchain_exec"]),
        cfg["objdump"],
        float(cfg["timeout"]),
        cfg["header"],
    )
    trial = run_trial(
        compiler, a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8"), control / "try"
    )
    typer.echo(trial.report())
    raise typer.Exit(0 if trial.ok else 1)


@learnings_app.command("curate")
def learnings_curate(
    batch: int = typer.Option(40, help="Candidates per curation run."),
    model: str | None = typer.Option(None),
    effort: str = typer.Option("medium"),
    yes: bool = typer.Option(False, "--yes", help="Confirm token spend."),
) -> None:
    """An agent curates pending candidates; rematch verifies its evidence before applying."""
    from rematch.learnings.curate_run import run_curation

    s = _session()
    _confirm_spend("learnings curate", 1, yes)
    conn = store.connect(s.db_path)
    try:
        applied, cost = run_curation(
            s, conn, batch, model or s.settings.worker_model, effort, _echo_err
        )
    finally:
        conn.close()
    typer.echo(f"accepted {applied.accepted}")
    typer.echo(f"dropped by the evidence check {applied.dropped}")
    typer.echo(f"merged {applied.merged}; rejected {len(applied.rejected)}")
    typer.echo(f"rewritten {applied.rewritten}; retired {applied.retired}")
    if applied.moved_to_target:
        typer.echo(f"moved to target scope (named target code): {applied.moved_to_target}")
    typer.echo(f"cost ${cost:.3f} (estimate)")


@learnings_app.command("verify")
def learnings_verify(
    retire: bool = typer.Option(False, help="Remove rules whose evidence no longer holds."),
) -> None:
    """Re-run every rule's evidence with the target compiler (no tokens)."""
    from rematch.learnings.curate_run import verify_all

    s = _session()
    results = verify_all(s, retire, _echo_err)
    for rid, ok, why in results:
        typer.echo(f"{'SKIP' if ok is None else 'PASS' if ok else 'FAIL'} {rid}: {why}")
    if any(ok is False for _, ok, _ in results):
        raise typer.Exit(1)
