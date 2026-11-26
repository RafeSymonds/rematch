"""`rematch` command-line entry point (SPEC §13)."""

from __future__ import annotations

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
def inventory() -> None:
    """Parse the vanilla build: every linked function, its features and difficulty, into the DB."""
    from rematch.inventory.run import run_inventory

    s = _session()
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
    mode: str = typer.Option("isolated", help="Compile mode (only isolated in Phase 1)."),
) -> None:
    """Compile candidate.c and diff it against the target (what ./check runs)."""
    from rematch.worker.check_entry import run_check

    if mode != "isolated":
        typer.echo(f"mode {mode!r} is not available yet")
        raise typer.Exit(2)
    text, code, _ = run_check(sandbox.resolve(), json_out)
    typer.echo(text)
    raise typer.Exit(code)


@app.command()
def ctx(
    function: str = typer.Argument(..., help="Name, 0xADDR or module:0xADDR."),
    out: Path = typer.Option(..., help="Directory to create."),
    with_source: bool = typer.Option(
        False, help="Seed candidate.c with the human C (c_human only; never for bench)."
    ),
) -> None:
    """Build a sandbox for one function without running a worker."""
    from rematch.worker.sandbox import SandboxError, build_sandbox, find_function

    s = _session()
    conn = store.connect(s.db_path)
    try:
        task = build_sandbox(s, find_function(conn, function), out, with_source)
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
