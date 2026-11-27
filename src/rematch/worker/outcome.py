"""Outcome classification after a worker exits (SPEC §7.4). Nothing the worker says is trusted."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from rematch.worker.check_entry import HISTORY, State, evaluate
from rematch.worker.invoke import InvokeResult
from rematch.worker.sandbox import verify_seal
from rematch.worker.task import Task

_BULLET = re.compile(r"^\s*[-*]\s+(?P<text>\S.*)$", re.MULTILINE)


@dataclass
class Classified:
    outcome: str
    checks_used: int = 0
    best_check: int | None = None
    best_score: float | None = None
    final_score: float | None = None
    best_source: str | None = None
    verified_versions: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)
    first_check_compiled: bool | None = None


def _history(sandbox: Path) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for path in sorted((sandbox / HISTORY).glob("[0-9][0-9][0-9].json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return out


def _score(entry: dict[str, object]) -> float:
    value = entry.get("score", 0.0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def classify(
    sandbox: Path,
    manifest: dict[str, str],
    invoke: InvokeResult,
    task: Task,
    control: Path | None = None,
) -> Classified:
    tampered = verify_seal(sandbox, manifest)
    if tampered:
        return Classified("invalid", detail=tampered)

    history = _history(sandbox)
    compiled = [h for h in history if h.get("status") in ("match", "nonmatch")]
    c = Classified("nonmatch", checks_used=len(history))
    if history:
        c.final_score = _score(history[-1])
        c.first_check_compiled = history[0].get("status") in ("match", "nonmatch")
    if compiled:
        best = max(compiled, key=lambda h: (_score(h), int(str(h.get("check", 0)))))
        c.best_check = int(str(best.get("check")))
        c.best_score = _score(best)
        source = (sandbox / HISTORY / f"{c.best_check:03d}.c").read_text(encoding="utf-8")
        c.best_source = source
        # Re-verify the best candidate ourselves; the history JSON is worker-writable.
        rejected, errors, results = evaluate(
            sandbox, task, source, sandbox / HISTORY / "verify", control
        )
        if rejected is None and not errors and results and all(r.match for r in results):
            c.outcome = "match"
            c.verified_versions = [r.version for r in results]
            c.best_score = 100.0
            return c
        if rejected or errors:
            c.detail.append(f"best candidate failed re-verification: {rejected or errors[:3]}")

    state = State.load(sandbox)
    stop = history[-1].get("stop") if history else None
    api_error = invoke.result is None or bool(invoke.result.get("is_error"))
    if not history and api_error:  # the worker never ran a check: not the function's fault
        c.outcome = "error"
        c.detail.append(str((invoke.result or {}).get("result", "")) or invoke.stderr_tail[-500:])
    elif not compiled:
        c.outcome = "compile_fail"
    elif isinstance(stop, str) and stop.startswith("no new best"):
        c.outcome = "stagnation"
    elif (isinstance(stop, str) and stop.startswith("check limit")) or (
        state.checks >= task.max_checks
    ):
        c.outcome = "check_limit"
    elif invoke.subtype == "error_max_turns":
        c.outcome = "turn_limit"
    elif invoke.timed_out:
        c.outcome = "timeout"
    elif invoke.subtype is not None and "budget" in invoke.subtype:
        c.outcome = "budget"
    elif invoke.result is None or bool(invoke.result.get("is_error")):
        c.outcome = "error"
        c.detail.append(invoke.stderr_tail[-500:])
    return c


def note_bullets(sandbox: Path) -> list[str]:
    path = sandbox / "NOTES.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")[:20000]
    return [m.group("text").strip()[:500] for m in _BULLET.finditer(text)][:20]
