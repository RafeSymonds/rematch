"""`rematch report` metrics (SPEC §11.3) and the target's benchmark log (CONTRIBUTING.md)."""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rematch.bench.sets import bucket

BENCHMARKS_FILE = "benchmarks.md"
HEADER = (
    "| date | target | run | set | prompt_v | context_v | model:effort | match % (S/M/L) "
    "| med checks | tokens/match | $/match (est) | notes |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
)


@dataclass
class Bucket:
    n: int = 0
    matches: int = 0
    first_check_compiled: int = 0
    checks_to_match: list[int] = field(default_factory=list)
    turns: list[int] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    cost: float = 0.0
    seconds: list[float] = field(default_factory=list)
    outcomes: dict[str, int] = field(default_factory=dict)

    @property
    def match_rate(self) -> float:
        return 100.0 * self.matches / self.n if self.n else 0.0

    def as_dict(self) -> dict[str, object]:
        tokens = self.input_tokens + self.output_tokens + self.cache_tokens
        return {
            "attempts": self.n,
            "matches": self.matches,
            "match_rate": round(self.match_rate, 1),
            "first_check_compile_rate": round(100.0 * self.first_check_compiled / self.n, 1)
            if self.n
            else 0.0,
            "median_checks_to_match": statistics.median(self.checks_to_match)
            if self.checks_to_match
            else None,
            "median_turns": statistics.median(self.turns) if self.turns else None,
            "mean_tokens_per_attempt": {
                "input": round(self.input_tokens / self.n) if self.n else 0,
                "output": round(self.output_tokens / self.n) if self.n else 0,
                "cache_read": round(self.cache_tokens / self.n) if self.n else 0,
            },
            "tokens_per_match": round(tokens / self.matches) if self.matches else None,
            "cost_per_match_usd": round(self.cost / self.matches, 3) if self.matches else None,
            "cost_total_usd": round(self.cost, 3),
            "median_wall_seconds": statistics.median(self.seconds) if self.seconds else None,
            "outcomes": self.outcomes,
            "lint_failure_rate": 0.0,  # §9 lints arrive in Phase 4
        }


def _first_compiled(sandbox: str | None) -> bool:
    if not sandbox:
        return False
    path = Path(sandbox) / ".history" / "001.json"
    try:
        return json.loads(path.read_text()).get("status") in ("match", "nonmatch")
    except (OSError, json.JSONDecodeError):
        return False


def _seconds(started: str | None, ended: str | None) -> float | None:
    if not started or not ended:
        return None
    return (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()


def run_metrics(conn: sqlite3.Connection, run_id: int) -> dict[str, object]:
    run = conn.execute(
        "SELECT kind, bench_set, config_json, started_at, ended_at FROM runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if run is None:
        raise KeyError(f"no run {run_id}")
    rows = conn.execute(
        "SELECT a.outcome, a.checks_used, a.num_turns, a.input_tokens, a.output_tokens,"
        " a.cache_read_tokens, a.cost_usd, a.started_at, a.ended_at, a.sandbox_path,"
        " a.best_candidate_path, f.n_insns, a.escalation FROM attempts a"
        " JOIN functions f ON f.id = a.function_id"
        " WHERE a.run_id = ?",
        (run_id,),
    ).fetchall()
    buckets: dict[str, Bucket] = {"all": Bucket(), "S": Bucket(), "M": Bucket(), "L": Bucket()}
    rungs: dict[int, Bucket] = {}  # by ladder rung (attempts.escalation, SPEC §8.4)
    for (
        outcome,
        _checks,
        turns,
        tin,
        tout,
        tcache,
        cost,
        started,
        ended,
        sandbox,
        best,
        n_insns,
        rung,
    ) in rows:
        by_rung = rungs.setdefault(int(rung or 0), Bucket())
        for b in (buckets["all"], buckets[bucket(int(n_insns or 0))], by_rung):
            b.n += 1
            b.outcomes[outcome or "unfinished"] = b.outcomes.get(outcome or "unfinished", 0) + 1
            b.first_check_compiled += int(_first_compiled(sandbox))
            b.input_tokens += tin or 0
            b.output_tokens += tout or 0
            b.cache_tokens += tcache or 0
            b.cost += cost or 0.0
            if turns is not None:
                b.turns.append(int(turns))
            secs = _seconds(started, ended)
            if secs is not None:
                b.seconds.append(secs)
            if outcome == "match":
                b.matches += 1
                if best:
                    b.checks_to_match.append(int(Path(best).stem))
    config = json.loads(run[2])
    return {
        "run": run_id,
        "kind": run[0],
        "set": run[1],
        "model": config.get("model"),
        "effort": config.get("effort"),
        "prompt_version": config.get("prompt_version"),
        "context_version": config.get("context_version"),
        "started_at": run[3],
        "ended_at": run[4],
        "buckets": {k: v.as_dict() for k, v in buckets.items()},
        "rungs": {str(k): v.as_dict() for k, v in sorted(rungs.items())},
    }


def format_report(m: dict[str, object]) -> str:
    buckets = m["buckets"]
    assert isinstance(buckets, dict)
    lines = [
        f"run {m['run']} ({m['kind']}, set {m['set']}) | {m['model']}:{m['effort']} | "
        f"prompt v{m['prompt_version']} context v{m['context_version']}"
    ]
    for name in ("all", "S", "M", "L"):
        b = buckets[name]
        lines.append(
            f"{name:>3}: {b['matches']}/{b['attempts']} match ({b['match_rate']}%), "
            f"first-check compile {b['first_check_compile_rate']}%, "
            f"median checks to match {b['median_checks_to_match']}, median turns "
            f"{b['median_turns']}, tokens/match {b['tokens_per_match']}, "
            f"$/match {b['cost_per_match_usd']}, median wall {b['median_wall_seconds']} s"
        )
    rungs = m.get("rungs") or {}
    assert isinstance(rungs, dict)
    if len(rungs) > 1 or m["kind"] == "live":
        for rung, b in rungs.items():
            lines.append(
                f"rung {rung}: {b['matches']}/{b['attempts']} match ({b['match_rate']}%),"
                f" $/match {b['cost_per_match_usd']}, total $ {b['cost_total_usd']}"
            )
    lines.append(f"outcomes: {buckets['all']['outcomes']}")
    return "\n".join(lines)


def benchmarks_row(m: dict[str, object], target: str, notes: str) -> str:
    b = m["buckets"]
    assert isinstance(b, dict)
    rates = "/".join(f"{b[k]['match_rate']:.0f}" for k in ("S", "M", "L"))
    a = b["all"]
    date = str(m.get("started_at") or "")[:10]
    return (
        f"| {date} | {target} | {m['run']} | {m['set']} | {m['prompt_version']} | "
        f"{m['context_version']} | {m['model']}:{m['effort']} | {a['match_rate']:.1f} ({rates}) "
        f"| {a['median_checks_to_match']} | {a['tokens_per_match']} | {a['cost_per_match_usd']} "
        f"| {notes} |\n"
    )


def append_row(target_dir: Path, row: str) -> Path:
    path = target_dir / BENCHMARKS_FILE
    if not path.is_file():
        path.write_text("# Benchmarks\n\nAppend-only (rematch CONTRIBUTING.md).\n\n" + HEADER)
    with path.open("a", encoding="utf-8") as f:
        f.write(row)
    return path
