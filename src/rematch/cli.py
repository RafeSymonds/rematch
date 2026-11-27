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
    """Check Claude Code, workdir isolation, vanilla builds, hashes and the DB."""
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
def pilot(
    n: int = typer.Option(50, help="Untried assembly functions to attempt."),
    seed: int = typer.Option(1),
    tu: list[str] = typer.Option([], "--tu", help="Attempt every untried function in these TUs."),
    workers: int | None = typer.Option(None),
    model: str | None = typer.Option(None),
    effort: str | None = typer.Option(None),
    yes: bool = typer.Option(False, "--yes", help="Confirm token spend."),
) -> None:
    """Live attempts on a size-stratified sample of untried assembly functions (spends tokens)."""
    from rematch.bench.report import format_report, run_metrics
    from rematch.bench.run import run_functions
    from rematch.bench.sets import live_sample
    from rematch.worker.sandbox import load_functions

    s = _session()
    conn = store.connect(s.db_path)
    try:
        if tu:
            marks = ",".join("?" * len(tu))
            ids = [
                int(r[0])
                for r in conn.execute(
                    f"SELECT id FROM functions WHERE source_kind = 'asm' AND status = 'untried'"
                    f" AND tu_path IN ({marks}) ORDER BY tu_path, address",
                    tu,
                )
            ]
        else:
            ids = live_sample(conn, n, seed)
        fns = load_functions(conn, ids)
    finally:
        conn.close()
    if not fns:
        typer.echo("no untried assembly functions")
        raise typer.Exit(1)
    m, e = _model_effort(model, effort)
    effort_for = {} if effort else _effort_by_size(s, [fn.id for fn in fns])
    _confirm_spend(f"pilot ({len(fns)} live functions)", len(fns), yes)
    result = run_functions(
        s,
        "live",
        fns,
        model=m,
        effort="by-size" if effort_for else e,
        workers=workers or s.settings.worker_count,
        bench=False,
        seed=seed,
        log=_echo_err,
        effort_for=effort_for,
        extra_config={
            "pilot": {"n": n, "seed": seed, "tus": tu, "function_ids": ids},
            "effort_by_size": s.settings.effort_by_size if effort_for else None,
        },
    )
    conn = store.connect(s.db_path)
    try:
        typer.echo(format_report(run_metrics(conn, result.run_id)))
    finally:
        conn.close()


@app.command()
def recheck(
    run: int = typer.Option(..., help="Live run whose non-matching attempts to re-verify."),
) -> None:
    """Re-verify a live run's misses with the current checker (no tokens)."""
    from rematch.worker.recheck import recheck_run

    s = _session()
    new_run, results = recheck_run(s, run, _echo_err)
    for r in results:
        typer.echo(f"{'MATCH' if r.matched else 'miss '} {r.function}: {r.detail}")
    n = sum(r.matched for r in results)
    typer.echo(f"{n}/{len(results)} recovered" + (f" (run {new_run})" if new_run else ""))


@app.command("integrate")
def integrate_cmd(
    tu: list[str] = typer.Option([], "--tu", help="Assembly TU(s) to integrate."),
    ready: bool = typer.Option(False, "--ready", help="Every TU with ready matches."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan and check pieces; write nothing."),
    fix: bool = typer.Option(False, "--fix", help="Run a tu_fix agent on failing pieces."),
    data: bool = typer.Option(
        False, "--data", help="tu_data: move fully converted TUs' data into C (with --fix)."
    ),
    types: bool = typer.Option(
        False, "--types", help="Merge duplicate provisional types in fully converted TUs."
    ),
    verify_branch_: bool = typer.Option(
        False, "--verify-branch", help="Build every rematch/live commit and check hashes."
    ),
    since: str | None = typer.Option(
        None, help="--verify-branch: start after this commit (default: last verified)."
    ),
    model: str | None = typer.Option(None),
    effort: str | None = typer.Option(None),
    yes: bool = typer.Option(False, "--yes", help="Confirm token spend (with --fix)."),
) -> None:
    """Move matched functions into C on rematch/live, verified by full builds (SPEC §10)."""
    from rematch.integrate.run import (
        IntegrateError,
        converted_tus,
        data_ready_tus,
        integrate,
        ready_tus,
        verify_branch,
    )
    from rematch.integrate.tu_data import run_tu_data
    from rematch.integrate.tu_fix import run_tu_fix

    s = _session()
    if verify_branch_:
        try:
            results = verify_branch(s, _echo_err, since)
        except IntegrateError as exc:
            typer.echo(f"FAIL {exc}")
            raise typer.Exit(1) from exc
        for sha, ok, detail in results:
            typer.echo(f"{'PASS' if ok else 'FAIL'} {sha[:10]} {detail[:300]}")
        if not all(ok for _, ok, _ in results):
            raise typer.Exit(1)
        return
    fixer = None
    if fix:
        _confirm_spend("tu_fix (one attempt per failing piece)", 1, yes)
        m, e = _model_effort(model, effort)

        def fixer(tu: str, check: object, asm: dict[str, str]) -> bool:
            from rematch.integrate.run import PieceCheck

            assert isinstance(check, PieceCheck) and check.control is not None
            fconn = store.connect(s.db_path)
            try:
                targets = {n: asm[n] for n in check.piece.functions}
                out = run_tu_fix(s, fconn, check.control, check.source, targets, m, e, _echo_err)
            finally:
                fconn.close()
            return out.matched

    data_fixer = None
    if fix and data:
        m, e = _model_effort(model, effort)

        def data_fixer(
            tu_path: str, functions: list[str], asm_piece: str, c_piece: str, asm: dict[str, str]
        ) -> bool:
            fconn = store.connect(s.db_path)
            try:
                out = run_tu_data(
                    s, fconn, tu_path, functions, asm_piece, c_piece, asm, m, e, _echo_err
                )
            finally:
                fconn.close()
            return out.matched

    conn = store.connect(s.db_path)
    try:
        if data:
            tus = list(tu) or data_ready_tus(conn)
        elif types:
            tus = list(tu) or converted_tus(conn)
        else:
            tus = list(tu) or (ready_tus(conn) if ready else [])
        if not tus:
            typer.echo("nothing to do: pass --tu, --ready, --types or --data")
            raise typer.Exit(2)
        try:
            outcomes = integrate(
                s,
                conn,
                tus,
                dry_run=dry_run,
                log=_echo_err,
                fix=fixer,
                step="data" if data else "types" if types else "functions",
                fix_data=data_fixer,
            )
        except IntegrateError as exc:
            typer.echo(f"FAIL {exc}")
            raise typer.Exit(1) from exc
    finally:
        conn.close()
    for o in outcomes:
        typer.echo(f"{o.status:9} {o.tu}: {o.detail}")
    if any(o.status == "failed" for o in outcomes):
        raise typer.Exit(1)


@app.command("run")
def run_cmd(
    workers: int | None = typer.Option(None),
    max_attempts: int | None = typer.Option(None, help="Stop launching after this many."),
    until: str | None = typer.Option(None, help="Stop launching at HH:MM (local time)."),
    integrate_every: int = typer.Option(10, help="Integrate ready TUs after this many matches."),
    no_integrate: bool = typer.Option(False, "--no-integrate"),
    fix: bool = typer.Option(False, "--fix", help="Let integration run tu_fix agents."),
    no_publish: bool = typer.Option(
        False, "--no-publish", help="Don't merge into MERGE_BRANCH or push at the end."
    ),
    finish: bool = typer.Option(
        False,
        "--finish",
        help="Also finish fully converted TUs: merge their provisional types, move their data"
        " into C (agents with --fix).",
    ),
    yes: bool = typer.Option(False, "--yes", help="Confirm token spend."),
) -> None:
    """The unattended live loop: attempt, escalate, integrate, publish (SPEC §8, §10)."""
    from datetime import datetime, timedelta

    from rematch.integrate.run import integrate, ready_tus
    from rematch.integrate.tu_fix import run_tu_fix
    from rematch.live.loop import run_live

    s = _session()
    stop_at = None
    if until:
        hh, mm = (int(x) for x in until.split(":"))
        local = datetime.now().astimezone()
        stop_at = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if stop_at <= local:
            stop_at += timedelta(days=1)
    _confirm_spend("live run", max_attempts or 0, yes)

    def fixer(tu: str, check: object, asm: dict[str, str]) -> bool:
        from rematch.integrate.run import PieceCheck

        assert isinstance(check, PieceCheck) and check.control is not None
        fconn = store.connect(s.db_path)
        try:
            m, e = _model_effort(None, None)
            targets = {n: asm[n] for n in check.piece.functions}
            return run_tu_fix(
                s, fconn, check.control, check.source, targets, m, e, _echo_err
            ).matched
        finally:
            fconn.close()

    def data_fixer(
        tu_path: str, functions: list[str], asm_piece: str, c_piece: str, asm: dict[str, str]
    ) -> bool:
        from rematch.integrate.tu_data import run_tu_data

        fconn = store.connect(s.db_path)
        try:
            m, e = _model_effort(None, None)
            return run_tu_data(
                s, fconn, tu_path, functions, asm_piece, c_piece, asm, m, e, _echo_err
            ).matched
        finally:
            fconn.close()

    def integrate_ready() -> list[str]:
        from rematch.integrate.run import converted_tus, data_ready_tus

        iconn = store.connect(s.db_path)
        try:
            outcomes = integrate(
                s,
                iconn,
                ready_tus(iconn),
                dry_run=False,
                log=_echo_err,
                fix=fixer if fix else None,
            )
            # finishing fully converted TUs: one type per object, then the data into C
            if finish:
                for step, tus in (
                    ("types", converted_tus(iconn)),
                    ("data", data_ready_tus(iconn)),
                ):
                    outcomes += integrate(
                        s,
                        iconn,
                        tus,
                        dry_run=False,
                        log=_echo_err,
                        fix=fixer if fix else None,
                        step=step,
                        fix_data=data_fixer if fix else None,
                    )
        finally:
            iconn.close()
        for o in outcomes:
            if o.status != "skipped":
                _echo_err(f"integrate {o.tu}: {o.status} {o.detail[:300]}")
        return [o.tu for o in outcomes if o.status == "committed"]

    stats = run_live(
        s,
        workers=workers or s.settings.worker_count,
        max_attempts=max_attempts,
        until=stop_at,
        integrate_every=integrate_every,
        integrate_fn=None if no_integrate else integrate_ready,
        log=_echo_err,
    )
    typer.echo(
        f"run {stats.run_id}: {stats.launched} attempts, {stats.matched} matched,"
        f" {stats.failed} failed, {stats.errors} errors; integrated {stats.integrated}"
    )
    if s.settings.merge_branch and not no_publish:
        _publish(s, None, None, dry_run=False)


def _publish(s: Session, branch: str | None, remote: str | None, dry_run: bool) -> None:
    from rematch.integrate.publish import PublishError, publish_live

    try:
        r = publish_live(s, _echo_err, branch, remote, dry_run)
    except PublishError as exc:
        typer.echo(f"FAIL publish: {exc}")
        raise typer.Exit(1) from exc
    what = "would move to" if dry_run else "at"
    merged = ", merged" + (" and built" if r.built else "") if r.merged else ""
    pushed = f", pushed to {r.pushed}" if r.pushed else ""
    typer.echo(f"publish: {r.branch} {what} {r.head[:10]}{merged}{pushed}")


@app.command("publish")
def publish_cmd(
    branch: str | None = typer.Option(None, help="Branch to merge into (default MERGE_BRANCH)."),
    remote: str | None = typer.Option(
        None, help="Remote to push the branch to (default PUSH_REMOTE; '' = don't push)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Say what would happen; change nothing."),
) -> None:
    """Merge rematch/live into a branch of the target and push it (SPEC §10.1)."""
    _publish(_session(), branch, remote, dry_run)


@app.command("status")
def status_cmd() -> None:
    """Live progress: function states, integrated share, recent integrations."""
    s = _session()
    conn = store.connect(s.db_path)
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*), SUM(n_insns) FROM functions WHERE source_kind = 'asm'"
            " GROUP BY status ORDER BY status"
        ).fetchall()
        total_n = sum(r[1] for r in rows)
        total_i = sum(r[2] or 0 for r in rows)
        typer.echo(f"assembly functions at the pin: {total_n} ({total_i} instructions)")
        for st, n, insns in rows:
            typer.echo(
                f"  {st:11} {n:6}  {100 * (insns or 0) / max(total_i, 1):5.2f}% of instructions"
            )
        tus = conn.execute(
            "SELECT COUNT(DISTINCT tu_path) FROM integrations WHERE status = 'committed'"
        ).fetchone()[0]
        whole = conn.execute(
            "SELECT COUNT(*) FROM (SELECT tu_path FROM functions WHERE source_kind = 'asm'"
            " GROUP BY tu_path HAVING SUM(status != 'integrated') = 0)"
        ).fetchone()[0]
        typer.echo(f"TUs with integrated code: {tus}; fully converted: {whole}")
        for tu, st, sha, detail, at in conn.execute(
            "SELECT tu_path, status, commit_sha, detail, created_at FROM integrations"
            " ORDER BY id DESC LIMIT 5"
        ):
            typer.echo(f"  {at} {st:9} {tu} {(sha or '')[:10]} {(detail or '')[:120]}")
    finally:
        conn.close()


@app.command("stuck")
def stuck_cmd(
    module: str | None = typer.Option(None, help="Only this module."),
    hint: str | None = typer.Option(None, help="Only functions whose best check had this hint."),
    show: str | None = typer.Option(None, help="Print one function's best candidate and diff."),
    accept: str | None = typer.Option(None, help="Record a human fix for this function."),
    source: Path | None = typer.Option(None, help="The C file for --accept."),
    limit: int = typer.Option(40, help="Rows to list."),
) -> None:
    """The review queue: failed and stuck functions, why they failed, human fixes (SPEC §8.4)."""
    from rematch.live.stuck import accept_human, hint_tally, stuck_rows

    s = _session()
    if accept:
        if source is None:
            typer.echo("--accept needs --source FILE")
            raise typer.Exit(2)
        ok, detail = accept_human(s, accept, source.read_text(encoding="utf-8"))
        typer.echo(f"{'MATCH' if ok else 'FAIL'} {accept}: {detail}")
        raise typer.Exit(0 if ok else 1)
    conn = store.connect(s.db_path)
    try:
        rows = stuck_rows(conn, module, hint)
    finally:
        conn.close()
    if show:
        row = next((r for r in rows if r.name == show), None)
        if row is None:
            typer.echo(f"{show} is not failed or stuck")
            raise typer.Exit(1)
        typer.echo(
            f"{row.name} ({row.tu}, {row.n_insns} insns): {row.attempts} attempt(s),"
            f" best {row.best_score:.1f}, ${row.cost:.2f}, hints {row.hints}"
        )
        for reason in row.reasons:
            typer.echo(f"  {reason[:300]}")
        if row.open_issues:
            typer.echo(f"open issues: {row.open_issues}")
        if row.best_candidate and Path(row.best_candidate).is_file():
            typer.echo(f"--- best candidate ({row.best_candidate})")
            typer.echo(Path(row.best_candidate).read_text(encoding="utf-8", errors="replace"))
        return
    typer.echo(f"{len(rows)} function(s); hints: {hint_tally(rows)}")
    for r in sorted(rows, key=lambda r: -r.best_score)[:limit]:
        typer.echo(
            f"{r.status:6} {r.best_score:5.1f} {r.n_insns:4}i {r.attempts}x"
            f" ${r.cost:5.2f} {r.name:24} {','.join(r.hints) or '-'}"
        )


@app.command("tu-check", hidden=True)
def tu_check_cmd(
    sandbox: Path = typer.Option(...),
    control: Path = typer.Option(...),
) -> None:
    """./check inside a tu_fix sandbox."""
    from rematch.integrate.tu_fix import tu_check

    text, code = tu_check(sandbox, control)
    typer.echo(text)
    raise typer.Exit(code)


@app.command("data-check", hidden=True)
def data_check_cmd(
    sandbox: Path = typer.Option(...),
    control: Path = typer.Option(...),
) -> None:
    """./check inside a tu_data sandbox."""
    from rematch.integrate.tu_data import data_check

    text, code = data_check(sandbox, control)
    typer.echo(text)
    raise typer.Exit(code)


@app.command("lint")
def lint_cmd() -> None:
    """Run the §9 readability lints over every stored match (no tokens)."""
    from rematch.lint.readability import lint

    s = _session()
    conn = store.connect(s.db_path)
    try:
        rows = conn.execute(
            "SELECT m.function_id, f.name, m.c_source FROM matches m"
            " JOIN functions f ON f.id = m.function_id ORDER BY f.name"
        ).fetchall()
        failed = 0
        with conn:
            for fid, name, source in rows:
                result = lint(source)
                conn.execute(
                    "UPDATE matches SET lint_json = ? WHERE function_id = ?",
                    (result.to_json(), fid),
                )
                if result.fail or result.flag:
                    typer.echo(
                        f"{'FAIL' if result.fail else 'flag'} {name}: "
                        + "; ".join(result.fail + result.flag)
                    )
                failed += not result.ok
    finally:
        conn.close()
    typer.echo(f"{len(rows) - failed}/{len(rows)} matches pass")


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


types_app = typer.Typer(no_args_is_help=True, help="Provisional and shared types (SPEC §16).")
app.add_typer(types_app, name="types")


@types_app.command("merge")
def types_merge(
    module: str | None = typer.Option(None, help="Only this module."),
    layout: bool = typer.Option(False, "--layout", help="Also link types by matching layout."),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Report clusters only."),
) -> None:
    """Cluster provisional types that are the same type, per module (SPEC §16.2)."""
    from rematch.types.merge import cluster, evidence, load_uses, stats

    s = _session()
    conn = store.connect(s.db_path)
    try:
        uses = load_uses(conn, module)
        modules = sorted({u.module for u in uses})
        sources: dict[str, dict[int, tuple[str, str]]] = {}
        for fid, name, mod, src in conn.execute(
            "SELECT f.id, f.name, f.module, m.c_source FROM functions f JOIN matches m"
            " ON m.function_id = f.id WHERE f.status IN ('matched', 'integrated')"
        ):
            sources.setdefault(str(mod), {})[int(fid)] = (str(name), str(src))
    finally:
        conn.close()
    total_multi = total = 0
    for mod in modules:
        mod_uses = [u for u in uses if u.module == mod]
        edges = evidence(mod_uses, sources.get(mod, {}), layout)
        clusters, refused = cluster(mod_uses, edges)
        multi = [c for c in clusters if len(c.members) > 1]
        total += len(mod_uses)
        total_multi += sum(len(c.members) for c in multi)
        if multi or module:
            typer.echo(f"{mod}: {stats(clusters, refused, edges)}")
    typer.echo(f"all: {total} types, {total_multi} in clusters of 2+")
    if not dry_run:
        typer.echo("the merge gate is not built yet; nothing changed")
        raise typer.Exit(2)
