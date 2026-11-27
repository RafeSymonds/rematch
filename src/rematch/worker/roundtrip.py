"""`rematch roundtrip`: human C compiled from its own source must match (Phase 1 exit criteria).

Each function gets a fresh sandbox whose candidate.c is the original C, and the real `./check`
runs on it, so the timing is the worker's. Golden sets and mutants come from the target's
`.rematch/golden.toml`.
"""

from __future__ import annotations

import json
import random
import sqlite3
import statistics
import subprocess
import time
import tomllib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from rematch.session import Session
from rematch.target.definition import target_dir
from rematch.worker.sandbox import (
    ContextError,
    FunctionRecord,
    SandboxError,
    SandboxOptions,
    build_sandbox,
    find_function,
)

GOLDEN_FILE = "golden.toml"
Log = Callable[[str], None]


class Mutant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    function: str
    find: str
    replace: str
    expect: str  # hint rule id that must fire, e.g. "signedness"


class Golden(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    functions: list[str]
    mutants: list[Mutant] = []


def load_golden(s: Session) -> Golden:
    path = target_dir(s.settings.target_repo) / GOLDEN_FILE
    if not path.is_file():
        raise SandboxError(f"{path} not found")
    return Golden.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))


@dataclass
class Outcome:
    function: str
    status: str  # match | nonmatch | compile_fail | rejected | error
    seconds: float  # wall time of ./check
    score: float = 0.0
    detail: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    expect: str | None = None  # mutants: expected hint
    ok: bool = False  # the outcome this run wanted


def sample_c_functions(conn: sqlite3.Connection, n: int, seed: int) -> list[FunctionRecord]:
    rows = conn.execute(
        "SELECT name, module, address FROM functions WHERE source_kind = 'c_human' "
        "AND status != 'skipped' ORDER BY module, address"
    ).fetchall()
    picked = random.Random(seed).sample(rows, min(n, len(rows)))
    return [find_function(conn, f"{module}:{address:#x}") for _, module, address in picked]


def _run_check(sandbox: Path) -> tuple[float, dict[str, object]]:
    start = time.perf_counter()
    subprocess.run(
        [str(sandbox / "check")], capture_output=True, text=True, timeout=300, check=False
    )
    seconds = time.perf_counter() - start
    history = sandbox / ".history" / "001.json"
    data = json.loads(history.read_text()) if history.is_file() else {"status": "error"}
    return seconds, data


def _outcome(fn: str, seconds: float, data: dict[str, object]) -> Outcome:
    status = str(data.get("status", "error"))
    detail: list[str] = []
    hints: list[str] = []
    errors = data.get("errors")
    if isinstance(errors, list):
        detail += [str(e) for e in errors]
    if data.get("reason"):
        detail.append(str(data["reason"]))
    versions = data.get("versions")
    if isinstance(versions, dict):
        for v in versions.values():
            if isinstance(v, dict):
                detail += [str(x) for x in v.get("reasons", [])]
                hints += [str(x) for x in v.get("hints", [])]
    score = data.get("score", 0.0)
    return Outcome(
        fn,
        status,
        round(seconds, 3),
        float(score) if isinstance(score, (int, float)) else 0.0,
        detail,
        hints,
    )


def roundtrip_one(s: Session, fn: FunctionRecord, sandbox: Path) -> Outcome:
    """Build a sandbox seeded with the human C and run the real ./check once."""
    try:
        build_sandbox(s, fn, sandbox, SandboxOptions(with_source=True))
    except ContextError as exc:
        return Outcome(fn.name, "context_error", 0.0, detail=[str(exc)])
    except SandboxError as exc:
        return Outcome(fn.name, "error", 0.0, detail=[str(exc)])
    seconds, data = _run_check(sandbox)
    out = _outcome(fn.name, seconds, data)
    out.ok = out.status == "match"
    return out


def roundtrip(
    s: Session, fns: list[FunctionRecord], root: Path, log: Log, workers: int = 1
) -> list[Outcome]:
    def one(item: tuple[int, FunctionRecord]) -> Outcome:
        i, fn = item
        out = roundtrip_one(s, fn, root / f"{i:03d}_{fn.name}")
        log(f"{i + 1}/{len(fns)} {fn.name}: {out.status} ({out.seconds:.2f}s)")
        return out

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, enumerate(fns)))


def run_mutants(
    s: Session, conn: sqlite3.Connection, mutants: list[Mutant], root: Path, log: Log
) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for i, mutant in enumerate(mutants):
        fn = find_function(conn, mutant.function)
        sandbox = root / f"m{i:02d}_{fn.name}"
        build_sandbox(s, fn, sandbox, SandboxOptions(with_source=True))
        candidate = (sandbox / "candidate.c").read_text(encoding="utf-8")
        if mutant.find not in candidate:
            outcomes.append(
                Outcome(
                    fn.name,
                    "error",
                    0.0,
                    detail=[f"{mutant.find!r} not found"],
                    expect=mutant.expect,
                )
            )
            continue
        (sandbox / "candidate.c").write_text(candidate.replace(mutant.find, mutant.replace, 1))
        seconds, data = _run_check(sandbox)
        out = _outcome(fn.name, seconds, data)
        out.expect = mutant.expect
        out.ok = out.status == "nonmatch" and mutant.expect in out.hints
        outcomes.append(out)
        log(f"mutant {i + 1}/{len(mutants)} {fn.name}: {out.status} hints={out.hints}")
    return outcomes


def summarize(outcomes: list[Outcome]) -> dict[str, object]:
    times = [o.seconds for o in outcomes if o.seconds > 0]
    return {
        "total": len(outcomes),
        "ok": sum(o.ok for o in outcomes),
        "by_status": {
            st: sum(1 for o in outcomes if o.status == st)
            for st in sorted({o.status for o in outcomes})
        },
        "median_check_seconds": round(statistics.median(times), 3) if times else None,
        "outcomes": [asdict(o) for o in outcomes],
    }
