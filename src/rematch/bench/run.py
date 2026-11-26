"""Run a list of functions through the worker pipeline (bench, selftest, single live attempt)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from rematch.context.examples import naive_examples, similar_examples
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


class _Gate:
    """Shared pause: after a rate limit, no worker starts a new attempt until it lifts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0

    def pause(self, seconds: float) -> None:
        with self._lock:
            self._until = max(self._until, time.monotonic() + seconds)

    def wait(self) -> None:
        while True:
            with self._lock:
                remaining = self._until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 30))


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
    effort_for: dict[int, str] | None = None,
) -> RunResult:
    effort_for = effort_for or {}
    conn = store.connect(s.db_path)
    try:
        config = run_config(
            s,
            model,
            effort,
            {
                "kind": kind,
                "seed": seed,
                "examples_mode": s.settings.examples_mode,
                **(extra_config or {}),
            },
        )
        run_id = start_run(conn, kind, config, bench_set)
        if s.settings.examples_mode == "similar":
            examples = {
                fn.id: similar_examples(s, conn, fn, s.settings.examples_k, bench, log)
                for fn in fns
            }
        else:
            examples = {fn.id: naive_examples(conn, fn, s.settings.examples_k, seed) for fn in fns}
    finally:
        conn.close()
    gate = _Gate()
    run_dir = s.settings.rematch_var / "runs" / str(run_id)
    prompt_file = system_prompt_file(s, run_dir)
    log(f"run {run_id} ({kind}): {len(fns)} functions, {workers} at a time, {model}:{effort}")

    def one(fn: FunctionRecord) -> tuple[int, FunctionRecord, Classified]:
        gate.wait()
        spec = AttemptSpec(
            run_id=run_id,
            kind=kind,
            fn=fn,
            options=SandboxOptions(bench=bench, examples=examples[fn.id]),
            model=model,
            effort=effort_for.get(fn.id, effort),
            system_prompt=prompt_file,
            claude_bin=claude_bin,
            gate=gate,
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
