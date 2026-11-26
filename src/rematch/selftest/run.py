"""`rematch selftest` (SPEC §14): the whole pipeline with fake agents and zero model calls."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from rematch.bench.pool import Prescreen, pool_sizes
from rematch.bench.run import run_functions
from rematch.bench.sets import stratified_sample
from rematch.db import store
from rematch.inventory.c_parser import function_source
from rematch.session import Session
from rematch.toolchain.compile_db import CompileDB
from rematch.worker.sandbox import FunctionRecord, anonymous_name, find_function

Log = Callable[[str], None]


@dataclasses.dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _wrapper(bin_dir: Path, name: str, module: str) -> Path:
    path = bin_dir / name
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m {module} "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_selftest(s: Session, n: int, workers: int, log: Log) -> list[Check]:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    root = s.settings.rematch_var / "selftest" / stamp
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    db_copy = root / "rematch.sqlite"
    shutil.copy(s.db_path, db_copy)
    # Own DB copy (attempt ids restart) and own workdir (so sandbox paths never collide).
    workdir = s.settings.rematch_workdir.expanduser() / "selftest" / stamp
    st = dataclasses.replace(
        s,
        settings=s.settings.model_copy(update={"rematch_db": db_copy, "rematch_workdir": workdir}),
    )

    conn = store.connect(db_copy)
    try:
        sizes = pool_sizes(conn, "clean", s.settings.bench_cutoff_date)
        prescreen = Prescreen(st, conn, log, workers)
        ids, _ = stratified_sample(sizes, n, seed=0, eligible=prescreen.eligible)
        fns: list[FunctionRecord] = []
        for fid in ids:
            module, address = conn.execute(
                "SELECT module, address FROM functions WHERE id = ?", (fid,)
            ).fetchone()
            fns.append(find_function(conn, f"{module}:{address:#x}"))
    finally:
        conn.close()

    answers: dict[str, str] = {}
    for fn in fns:
        text = (s.worktree / fn.tu_path).read_text(encoding="utf-8", errors="replace")
        source = function_source(text, fn.name)
        assert source is not None
        symbol = anonymous_name(s, fn.module, fn.address)
        answers[symbol] = re.sub(rf"\b{re.escape(fn.name)}\b", symbol, source) + "\n"
    (root / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    fake_log = root / "fake_calls.jsonl"
    types_symbol = anonymous_name(s, fns[0].module, fns[0].address)

    build = s.definition.require(s.definition.build_info, "build")
    db = CompileDB.load(
        s.definition.build_dir(s.worktree, s.primary_version) / build.compile_commands, s.worktree
    )
    objects = sorted({db.entry(fn.tu_path).object_path for fn in fns})
    before = {str(p): _sha(p) for p in objects}
    vanilla_file = s.worktree / fns[0].tu_path

    os.environ.update(
        {
            "REMATCH_ORACLE_ANSWERS": str(root / "answers.json"),
            "REMATCH_FAKE_LOG": str(fake_log),
            "REMATCH_ORACLE_TYPES_FOR": types_symbol,
            "REMATCH_SABOTEUR_VANILLA": str(vanilla_file),
        }
    )
    oracle = _wrapper(bin_dir, "claude-oracle", "rematch.selftest.oracle_agent")
    saboteur = _wrapper(bin_dir, "claude-saboteur", "rematch.selftest.saboteur_agent")

    oracle_run = run_functions(
        st,
        "selftest",
        fns,
        model="fake",
        effort="none",
        workers=workers,
        bench=True,
        seed=0,
        log=log,
        claude_bin=str(oracle),
    )
    sabotage_run = run_functions(
        st,
        "selftest",
        fns[:1],
        model="fake",
        effort="none",
        workers=1,
        bench=True,
        seed=0,
        log=log,
        claude_bin=str(saboteur),
    )

    checks: list[Check] = []
    matches = [c for _, _, c in oracle_run.attempts if c.outcome == "match"]
    others = [
        (f.name, c.outcome, c.detail[:2]) for _, f, c in oracle_run.attempts if c.outcome != "match"
    ]
    checks.append(
        Check(
            "oracle",
            len(matches) == len(fns),
            f"{len(matches)}/{len(fns)} match" + (f"; others {others}" if others else ""),
        )
    )
    sab = sabotage_run.attempts[0][2]
    checks.append(Check("saboteur", sab.outcome == "invalid", f"{sab.outcome}: {sab.detail[:4]}"))

    calls = [json.loads(line) for line in fake_log.read_text().splitlines() if line.strip()]
    expected = len(fns) + 1
    real_bin = shutil.which(s.settings.claude_bin) or s.settings.claude_bin
    checks.append(
        Check(
            "zero model calls",
            len(calls) == expected and all(c["agent"] in ("oracle", "saboteur") for c in calls),
            f"{len(calls)} fake invocations for {expected} attempts; {real_bin} never run",
        )
    )

    sab_log: dict[str, list[str]] = next(
        (c for c in calls if c["agent"] == "saboteur"), {"results": []}
    )
    denied = [r for r in sab_log["results"] if "vanilla" in r]
    after = {p: _sha(Path(p)) for p in before}
    status = subprocess.run(
        ["git", "-C", str(s.worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()
    checks.append(
        Check(
            "vanilla unchanged",
            after == before and not status and all("denied" in r for r in denied) and bool(denied),
            f"{len(before)} target objects identical, git status {status or 'clean'!r},"
            f" saboteur: {denied}",
        )
    )

    conn = store.connect(db_copy)
    try:
        rows = conn.execute(
            "SELECT p.name, p.size, p.signature FROM provisional_types p "
            "JOIN functions f ON f.id = p.function_id WHERE f.id = ?",
            (fns[0].id,),
        ).fetchall()
    finally:
        conn.close()
    checks.append(
        Check(
            "provisional types",
            any(size == 12 and sig == "0:3,4:2,8:4" for _, size, sig in rows),
            f"stored {rows}",
        )
    )
    log(f"selftest files: {root}")
    return checks
