"""`rematch` command-line entry point (SPEC §13)."""

from __future__ import annotations

import typer
from pydantic import ValidationError

from rematch.config import Settings
from rematch.doctor import iter_doctor

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Agent harness for matching decompilation."""


@app.command()
def doctor() -> None:
    """Check Claude Code, workdir isolation, push guard, vanilla builds, hashes and the DB."""
    try:
        settings = Settings()  # type: ignore[call-arg]  # required keys come from the environment
    except ValidationError as exc:
        typer.echo(f"FAIL config: {exc}")
        raise typer.Exit(1) from exc

    failed = False
    for result in iter_doctor(settings, log=lambda msg: typer.echo(f"  .. {msg}", err=True)):
        typer.echo(f"{'PASS' if result.ok else 'FAIL'} {result.name}: {result.detail}")
        failed |= not result.ok
    if failed:
        raise typer.Exit(1)
