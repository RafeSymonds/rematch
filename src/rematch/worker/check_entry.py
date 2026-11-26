"""`rematch cd` / `./check`: compile candidate.c, diff against the vanilla target (SPEC §6.4).

Reads only the sandbox (`task.json`, `ctx*.h`, `.target/`, `candidate.c`) and writes only
`.history/` and the scratch files. Exit codes: 0 MATCH, 1 no match, 3 STOP.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rematch.diff.hints import hints
from rematch.diff.match import FunctionView, RelocEntry, binary_match, function_view
from rematch.diff.objdump_backend import Line, disassemble, run_objdump
from rematch.diff.render import VersionResult, display_score, render_match, render_result
from rematch.diff.score import align, score
from rematch.inventory.c_parser import mask, parse_c_functions
from rematch.toolchain.compile import compile_candidate, scratch_source
from rematch.toolchain.objects import read_object
from rematch.toolchain.runner import ToolchainRunner
from rematch.worker.task import Task

HISTORY = ".history"
TARGET_CACHE = ".target"
EXIT_MATCH, EXIT_NO_MATCH, EXIT_STOP = 0, 1, 3

_FORBIDDEN = (
    (re.compile(r"\b__asm\b|\basm\b"), "inline asm"),
    (re.compile(r"^\s*#\s*pragma\b", re.MULTILINE), "#pragma"),
)
_INCLUDE = re.compile(r'^\s*#\s*include\s*(?P<what>[<"][^>"]*[>"])', re.MULTILINE)
_COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)


# --- target cache (written by the sandbox builder) -----------------------------------


def save_target(sandbox: Path, version: str, lines: list[Line], view: FunctionView) -> None:
    path = sandbox / TARGET_CACHE / f"{version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "lines": [asdict(line) for line in lines],
        "view": {
            "name": view.name,
            "size": view.size,
            "code": view.code.hex(),
            "relocs": [asdict(r) for r in view.relocs],
        },
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def load_target(sandbox: Path, version: str) -> tuple[list[Line], FunctionView]:
    data = json.loads((sandbox / TARGET_CACHE / f"{version}.json").read_text(encoding="utf-8"))
    lines = [Line(**item) for item in data["lines"]]
    v = data["view"]
    view = FunctionView(
        v["name"], v["size"], bytes.fromhex(v["code"]), tuple(RelocEntry(**r) for r in v["relocs"])
    )
    return lines, view


# --- pre-filter ----------------------------------------------------------------------


def prefilter(candidate: str, symbol: str) -> str | None:
    """Reason to reject without compiling (SPEC §6.4 step 1), or None."""
    no_comments = _COMMENTS.sub(" ", candidate)
    masked = mask(candidate)
    for pattern, what in _FORBIDDEN:
        text = no_comments if what == "#pragma" else masked
        if pattern.search(text):
            return f"{what} is not allowed"
    for m in _INCLUDE.finditer(no_comments):
        if m.group("what") != '"ctx.h"':
            return f"#include {m.group('what')} is not allowed (ctx.h is already included)"
    count = sum(1 for f in parse_c_functions(candidate) if f.name == symbol)
    if count == 0:
        return f"candidate.c does not define {symbol}"
    if count > 1:
        return f"candidate.c defines {symbol} {count} times"
    return None


def keep_reference(candidate: str, symbol: str) -> str:
    """Force a static definition to be emitted: nothing references it in isolation."""
    for f in parse_c_functions(candidate):
        if f.name == symbol and re.search(r"\bstatic\b", f.header(candidate)):
            return f"\nvoid *const rematch_keep_{symbol} = (void *){symbol};\n"
    return ""


# --- state ---------------------------------------------------------------------------


@dataclass
class State:
    checks: int = 0
    best_score: float = -1.0
    best_check: int = 0
    since_best: int = 0

    @classmethod
    def load(cls, sandbox: Path) -> State:
        path = sandbox / HISTORY / "state.json"
        if not path.is_file():
            return cls()
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, sandbox: Path) -> None:
        path = sandbox / HISTORY / "state.json"
        path.write_text(json.dumps(asdict(self)), encoding="utf-8")


@dataclass
class CheckResult:
    check: int
    status: str  # match | nonmatch | compile_fail | rejected
    score: float
    match: bool
    seconds: float
    versions: dict[str, dict[str, object]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    reason: str | None = None
    stop: str | None = None


def _stop_reason(state: State, task: Task) -> str | None:
    if state.checks >= task.max_checks:
        return f"check limit ({task.max_checks}) reached"
    if state.since_best >= task.stagnation_limit:
        return f"no new best score in {task.stagnation_limit} checks"
    return None


def stop_line(reason: str) -> str:
    return f"STOP: {reason}. Write SUMMARY.json and end."


# --- the check -----------------------------------------------------------------------


def run_check(sandbox: Path, json_out: Path | None = None) -> tuple[str, int, CheckResult | None]:
    start = time.perf_counter()
    task = Task.load(sandbox)
    (sandbox / HISTORY).mkdir(exist_ok=True)
    state = State.load(sandbox)
    reason = _stop_reason(state, task)
    if reason is not None:
        return stop_line(reason), EXIT_STOP, None

    state.checks += 1
    n = state.checks
    candidate = (sandbox / "candidate.c").read_text(encoding="utf-8", errors="replace")
    (sandbox / HISTORY / f"{n:03d}.c").write_text(candidate, encoding="utf-8")

    rejected = prefilter(candidate, task.symbol)
    if rejected is not None:
        result = CheckResult(n, "rejected", 0.0, False, 0.0, reason=rejected)
        text = f"check {n}/{task.max_checks} | REJECTED: {rejected}"
        return _finish(sandbox, task, state, result, text, start, json_out)

    runner = ToolchainRunner.from_spec(task.toolchain_exec)
    results: list[VersionResult] = []
    errors: list[str] = []
    for vt in task.versions:
        scratch = sandbox / f"scratch.{vt.version}.c"
        scratch.write_text(
            scratch_source(candidate, vt.ctx) + keep_reference(candidate, task.symbol),
            encoding="utf-8",
        )
        compiled = compile_candidate(
            vt.compile.entry(),
            runner,
            scratch,
            sandbox / f"candidate.{vt.version}.o",
            task.compile_timeout,
        )
        if not compiled.ok:
            errors = list(compiled.errors)
            break
        cobj = read_object(compiled.object_path)
        clines = disassemble(
            run_objdump(task.objdump, compiled.object_path), cobj, {task.symbol: task.symbol}
        ).get(task.symbol)
        if clines is None:
            errors = [f"{task.symbol} was not emitted by the compiler"]
            break
        tlines, tview = load_target(sandbox, vt.version)
        cview = function_view(cobj, task.symbol, clines, task.symbol)
        m = binary_match(tview, cview)
        ops = align(tlines, clines)
        n_t = sum(1 for x in tlines if not x.is_data)
        n_c = sum(1 for x in clines if not x.is_data)
        results.append(
            VersionResult(
                vt.version,
                m.match,
                score(ops, len(tlines), len(clines)),
                n_t,
                n_c,
                ops,
                hints(ops, task.mode),
                m.reasons,
            )
        )

    if errors:
        result = CheckResult(n, "compile_fail", 0.0, False, 0.0, errors=errors)
        text = "\n".join([f"check {n}/{task.max_checks} | COMPILE ERROR", *errors])
        return _finish(sandbox, task, state, result, text, start, json_out)

    current = min(display_score(r) for r in results)
    matched = all(r.match for r in results)
    result = CheckResult(
        n,
        "match" if matched else "nonmatch",
        100.0 if matched else current,
        matched,
        0.0,
        versions={
            r.version: {
                "match": r.match,
                "score": display_score(r),
                "reasons": list(r.reasons),
                "hints": [rule for rule, _ in r.hints],
            }
            for r in results
        },
    )
    best = max(state.best_score, result.score)
    text = (
        render_match(n, task.max_checks)
        if matched
        else render_result(n, task.max_checks, current, best, results)
    )
    return _finish(sandbox, task, state, result, text, start, json_out)


def _finish(
    sandbox: Path,
    task: Task,
    state: State,
    result: CheckResult,
    text: str,
    start: float,
    json_out: Path | None,
) -> tuple[str, int, CheckResult]:
    if result.score > state.best_score:
        state.best_score, state.best_check, state.since_best = result.score, result.check, 0
    else:
        state.since_best += 1
    state.save(sandbox)
    result.seconds = round(time.perf_counter() - start, 3)
    if not result.match:
        result.stop = _stop_reason(state, task)
    payload = json.dumps(asdict(result), indent=2)
    (sandbox / HISTORY / f"{result.check:03d}.json").write_text(payload, encoding="utf-8")
    if json_out is not None:
        json_out.write_text(payload, encoding="utf-8")
    if result.match:
        return text, EXIT_MATCH, result
    if result.stop is not None:
        return f"{text}\n{stop_line(result.stop)}", EXIT_STOP, result
    return text, EXIT_NO_MATCH, result
