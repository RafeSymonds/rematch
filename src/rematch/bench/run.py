"""Run a list of functions through the worker pipeline (bench, selftest, single live attempt)."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from rematch.context.examples import naive_examples
from rematch.db import store
from rematch.session import Session
from rematch.worker.attempt import (
    REMATCH_ROOT,
    AttemptSpec,
    finish_run,
    run_attempt,
    run_config,
    start_run,
    system_prompt_file,
)
from rematch.worker.outcome import Classified
from rematch.worker.sandbox import FunctionRecord, SandboxOptions

Log = Callable[[str], None]


@dataclass(frozen=True)
class RunResult:
    run_id: int
    attempts: list[tuple[int, FunctionRecord, Classified]]


def run_functions(
    s: Session,
    kind: str,
    fns: list[FunctionRecord],
    *,
    model: str,
    effort: str,
    workers: int,
    bench: bool,
    seed: int,
    log: Log,
    bench_set: str | None = None,
    claude_bin: str | None = None,
    extra_config: dict[str, object] | None = None,
) -> RunResult:
    conn = store.connect(s.db_path)
    try:
        config = run_config(s, model, effort, {"kind": kind, "seed": seed, **(extra_config or {})})
        run_id = start_run(conn, kind, config, bench_set)
        examples = {fn.id: naive_examples(conn, fn, s.settings.examples_k, seed) for fn in fns}
    finally:
        conn.close()
    run_dir = s.settings.rematch_var / "runs" / str(run_id)
    prompt_file = system_prompt_file(s, run_dir)
    log(f"run {run_id} ({kind}): {len(fns)} functions, {workers} at a time, {model}:{effort}")

    def one(fn: FunctionRecord) -> tuple[int, FunctionRecord, Classified]:
        spec = AttemptSpec(
            run_id=run_id,
            kind=kind,
            fn=fn,
            options=SandboxOptions(bench=bench, examples=examples[fn.id]),
            model=model,
            effort=effort,
            system_prompt=prompt_file,
            claude_bin=claude_bin,
        )
        try:
            attempt_id, c = run_attempt(s, spec, log)
        except Exception as exc:  # one broken attempt must not stop an unattended run
            log(f"{fn.name}: harness error {exc!r}")
            return -1, fn, Classified("error", detail=[repr(exc)])
        return attempt_id, fn, c

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(one, fns))
    conn = store.connect(s.db_path)
    try:
        finish_run(conn, run_id)
    finally:
        conn.close()
    return RunResult(run_id, results)


__all__ = ["REMATCH_ROOT", "RunResult", "run_functions"]
