"""`rematch integrate`: land matched functions on rematch/live (SPEC §10, Phase 4a).

Per assembly TU, the functions in C after this step are the ones already integrated plus the
ready matches (lint-clean) that form runs of at least INTEGRATE_MIN_RUN or complete the TU. The
TU's pieces are regenerated from the vanilla source for that set, so earlier pieces merge with
new neighbours. Every C piece is compiled and each of its functions binary-matched against the
vanilla object before anything is written; then every version is built in the live worktree and
its ROM hash checked. Pass: commit. Fail: restore the worktree and record why.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from rematch.context.header_index import HeaderIndex
from rematch.diff.match import function_view
from rematch.doctor import sha1_file
from rematch.integrate.compose import compose_piece
from rematch.integrate.linkspec import WRITERS
from rematch.integrate.piece import PieceSpec, check_source, save_targets
from rematch.integrate.pret_asm import ParsedTU, Piece, SplitError, parse_tu, plan_pieces
from rematch.integrate.pret_asm import render_asm_pieces as render_pret_pieces
from rematch.lint.readability import passes
from rematch.session import Session
from rematch.toolchain.compile_db import CompileDB
from rematch.worker.attempt import now
from rematch.worker.sandbox import FunctionRecord, _vanilla_lines, context_stub
from rematch.worker.task import CompileSpec

Log = Callable[[str], None]


class IntegrateError(Exception):
    pass


@dataclass
class TUPlan:
    tu_path: str
    in_c: list[str]  # every function in C after this step, address order
    new: list[str]  # functions this step adds
    pieces: list[Piece]
    old_pieces: list[str]


@dataclass
class PieceCheck:
    piece: Piece
    ok: bool
    problems: list[str] = field(default_factory=list)
    undefined: set[str] = field(default_factory=set)
    source: str = ""
    control: Path | None = None  # piece.json and targets, for a tu_fix attempt


# --- state -----------------------------------------------------------------------------


def current_pieces(conn: sqlite3.Connection, tu: str) -> list[str]:
    row = conn.execute(
        "SELECT pieces_json FROM integrations WHERE tu_path = ? AND status = 'committed'"
        " ORDER BY id DESC LIMIT 1",
        (tu,),
    ).fetchone()
    return list(json.loads(row[0])) if row else [tu]


def _tu_functions(conn: sqlite3.Connection, tu: str) -> list[tuple[str, str, str | None]]:
    """(name, status, lint_json) for the TU's functions."""
    return [
        (str(n), str(st), lj)
        for n, st, lj in conn.execute(
            "SELECT f.name, f.status, m.lint_json FROM functions f"
            " LEFT JOIN matches m ON m.function_id = f.id WHERE f.tu_path = ?",
            (tu,),
        )
    ]


def ready_tus(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT f.tu_path FROM functions f JOIN matches m ON m.function_id = f.id"
        " WHERE f.source_kind = 'asm' AND f.status = 'matched' ORDER BY f.tu_path"
    ).fetchall()
    return [str(r[0]) for r in rows]


def plan_tu(s: Session, conn: sqlite3.Connection, tu: str, parsed: ParsedTU) -> TUPlan | None:
    order = [f.name for f in parsed.functions]
    info = {n: (st, lj) for n, st, lj in _tu_functions(conn, tu)}
    done = {n for n in order if info.get(n, ("", None))[0] == "integrated"}
    ready = {
        n for n in order if info.get(n, ("", None))[0] == "matched" and passes(info[n][1] or "")
    }
    # runs over done and ready: new functions join through a long enough or TU-complete run
    chosen = set(done)
    run: list[str] = []
    for n in [*order, None]:
        if n is not None and n in done | ready:
            run.append(n)
            continue
        if run:
            whole = len(run) == len(order)
            if whole or len(run) >= s.settings.integrate_min_run:
                chosen.update(run)
            run = []
    new = [n for n in order if n in chosen and n not in done]
    if not new:
        return None
    in_c = [n for n in order if n in chosen]
    return TUPlan(tu, in_c, new, plan_pieces(tu, parsed, set(in_c)), current_pieces(conn, tu))


# --- checks ----------------------------------------------------------------------------


def stored_source(conn: sqlite3.Connection, tu: str, functions: list[str]) -> str | None:
    row = conn.execute(
        "SELECT source FROM piece_sources WHERE tu_path = ? AND functions_json = ?"
        " ORDER BY id DESC LIMIT 1",
        (tu, json.dumps(functions)),
    ).fetchone()
    return str(row[0]) if row else None


def prepare_piece(
    s: Session,
    tu: str,
    tu_text: str,
    parsed: ParsedTU,
    piece: Piece,
    sources: dict[str, str],
    control: Path,
) -> tuple[PieceSpec, str]:
    """Write the piece's control data (targets per function and version) and compose it."""
    index = (
        HeaderIndex.from_json(s.header_index_path.read_text(encoding="utf-8"))
        if s.header_index_path.is_file()
        else None
    )
    build = s.definition.require(s.definition.build_info, "build")
    asm_text = {f.name: f.text for f in parsed.functions}
    includes: list[str] = []
    compile_specs: dict[str, dict[str, object]] = {}
    offsets: dict[str, dict[str, int]] = {}
    for vi, version in enumerate(s.target.versions()):
        db = CompileDB.load(
            s.definition.build_dir(s.worktree, version) / build.compile_commands, s.worktree
        )
        compile_specs[version] = CompileSpec.of(db.c_template_for(tu)).model_dump()
        offsets[version] = {}
        first: int | None = None
        for name in piece.functions:
            vobj, lines = _vanilla_lines(s, db.entry(tu).object_path, name, asm_text[name])
            vsym = vobj.function(name)
            assert vsym is not None
            first = vsym.value if first is None else first
            offsets[version][name] = vsym.value - first
            save_targets(control, version, name, lines, function_view(vobj, name, lines, name))
            if vi == 0:
                callees = [ln.reloc for ln in lines if ln.reloc and not ln.reloc.startswith("<")]
                stub = context_stub(s, _fn_record(name, tu), tu_text, callees, index)
                includes += [ln.split('"')[1] for ln in stub.splitlines() if '"' in ln]
    spec = PieceSpec(
        tu,
        piece.functions,
        compile_specs,
        offsets,
        s.settings.toolchain_exec,
        s.settings.objdump,
        s.settings.compile_timeout_sec,
        s.settings.max_checks,
    )
    spec.save(control)
    return spec, compose_piece(includes, [(n, sources[n]) for n in piece.functions])


def check_piece(
    s: Session,
    conn: sqlite3.Connection,
    tu: str,
    tu_text: str,
    parsed: ParsedTU,
    piece: Piece,
    sources: dict[str, str],
    work: Path,
) -> PieceCheck:
    """Compose (or take the stored tu_fix source) and check a C piece for every version."""
    control = work / Path(piece.path).stem / "control"
    spec, composed = prepare_piece(s, tu, tu_text, parsed, piece, sources, control)
    stored = stored_source(conn, tu, piece.functions)
    source = stored or composed
    r = check_source(spec, control, source, control.parent / "build")
    problems = r.errors + [
        f"{f.name}: {f.score:.1f} {'; '.join(f.detail[:2])}" for f in r.functions if not f.match
    ]
    return PieceCheck(piece, r.ok, problems, r.undefined, source, control)


def _fn_record(name: str, tu: str) -> FunctionRecord:
    return FunctionRecord(0, name, 0, "", tu, "asm", "thumb", False)


# --- the live worktree -----------------------------------------------------------------


def _git(live: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(live), *args], capture_output=True, text=True, timeout=120
    )
    if out.returncode != 0:
        raise IntegrateError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout.strip()


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IntegrateError(f"another integration holds {path}") from exc
        yield


def live_worktree(s: Session) -> Path:
    live = s.settings.live_worktree
    if live is None or not live.is_dir():
        raise IntegrateError("LIVE_WORKTREE is not set to a worktree of TARGET_REPO")
    if _git(live, "rev-parse", "--abbrev-ref", "HEAD") != "rematch/live":
        raise IntegrateError(f"{live} is not on branch rematch/live")
    if _git(live, "status", "--porcelain", "--untracked-files=normal"):
        raise IntegrateError(f"{live} has uncommitted changes")
    return live


def _build_all(s: Session, live: Path, log: Log) -> list[str]:
    problems = []
    for version in s.target.versions():
        log(f"building {version}")
        try:
            rom = s.target.build(live, version)
        except Exception as exc:  # BuildError, timeouts: all mean "do not commit"
            problems.append(f"{version}: {exc}"[:2000])
            continue
        actual = sha1_file(rom)
        if actual != s.target.expected_sha1(version):
            problems.append(f"{version}: sha1 {actual}, expected {s.target.expected_sha1(version)}")
    return problems


def _restore(live: Path, written: list[str]) -> None:
    _git(live, "reset", "-q", "--hard", "HEAD")
    for rel in written:
        p = live / rel
        if p.is_file() and not _git(live, "ls-files", rel):
            p.unlink()


def _record(
    conn: sqlite3.Connection, plan: TUPlan, status: str, sha: str | None, detail: str
) -> None:
    pieces = [p.path for p in plan.pieces] if status == "committed" else plan.old_pieces
    with conn:
        conn.execute(
            "INSERT INTO integrations (tu_path, status, functions_json, pieces_json, commit_sha,"
            " detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (plan.tu_path, status, json.dumps(plan.in_c), json.dumps(pieces), sha, detail, now()),
        )
        if status == "committed":
            marks = ",".join("?" * len(plan.new))
            conn.execute(
                f"UPDATE functions SET status = 'integrated', updated_at = ?"
                f" WHERE tu_path = ? AND name IN ({marks})",
                (now(), plan.tu_path, *plan.new),
            )
            conn.execute(
                f"UPDATE matches SET integrated_commit = ? WHERE function_id IN"
                f" (SELECT id FROM functions WHERE tu_path = ? AND name IN ({marks}))",
                (sha, plan.tu_path, *plan.new),
            )


@dataclass
class Outcome:
    tu: str
    status: str  # committed | failed | planned | skipped
    detail: str
    new: list[str] = field(default_factory=list)


def integrate(
    s: Session,
    conn: sqlite3.Connection,
    tus: list[str],
    *,
    dry_run: bool,
    log: Log,
    fix: Callable[[str, PieceCheck, dict[str, str]], bool] | None = None,
) -> list[Outcome]:
    """`fix(tu, failed_check, asm_by_function)` runs a tu_fix attempt; True if it stored a
    passing source, after which the TU's pieces are checked once more."""
    if s.definition.asm is None or s.definition.asm.format != "pret-macros":
        raise IntegrateError("integration needs [asm] format = 'pret-macros'")
    link = s.definition.require(s.definition.link_order, "link_order")
    writer = WRITERS.get(link.format)
    if writer is None:
        raise IntegrateError(f"no link-order writer for {link.format!r}")
    live = None if dry_run else live_worktree(s)
    outcomes: list[Outcome] = []
    with _lock(s.settings.rematch_var / "integrate.lock"):
        for tu in tus:
            tu_text = (s.worktree / tu).read_text(encoding="utf-8", errors="replace")
            try:
                parsed = parse_tu(tu_text)
            except SplitError as exc:
                outcomes.append(Outcome(tu, "skipped", f"cannot split: {exc}"))
                continue
            plan = plan_tu(s, conn, tu, parsed)
            if plan is None:
                outcomes.append(Outcome(tu, "skipped", "no run ready"))
                continue
            sources = dict(
                conn.execute(
                    "SELECT f.name, m.c_source FROM matches m JOIN functions f"
                    " ON f.id = m.function_id WHERE f.tu_path = ?",
                    (tu,),
                ).fetchall()
            )
            work = s.settings.rematch_workdir.expanduser() / "integrate" / tu.replace("/", "_")
            checks = [
                check_piece(s, conn, tu, tu_text, parsed, p, sources, work)
                for p in plan.pieces
                if p.kind == "c"
            ]
            bad = [c for c in checks if not c.ok]
            if bad and fix is not None:
                asm = {f.name: f.text for f in parsed.functions}
                if all(fix(tu, c, asm) for c in bad):
                    checks = [
                        check_piece(s, conn, tu, tu_text, parsed, p, sources, work)
                        for p in plan.pieces
                        if p.kind == "c"
                    ]
                    bad = [c for c in checks if not c.ok]
            if bad:
                detail = "; ".join(f"{c.piece.path}: {' | '.join(c.problems[:3])}" for c in bad)
                outcomes.append(Outcome(tu, "failed", f"piece check: {detail}", plan.new))
                if not dry_run:
                    _record(conn, plan, "failed", None, f"piece check: {detail}"[:4000])
                continue
            c_undefined = set().union(*(c.undefined for c in checks))
            asm_texts = render_pret_pieces(parsed, plan.pieces, c_undefined)
            if dry_run or live is None:
                desc = ", ".join(f"{p.path} ({len(p.functions)})" for p in plan.pieces)
                outcomes.append(Outcome(tu, "planned", desc, plan.new))
                continue
            written = [p.path for p in plan.pieces]
            for old in plan.old_pieces:
                if old not in written:
                    _git(live, "rm", "-q", old)
            for c in checks:
                (live / c.piece.path).write_text(c.source, encoding="utf-8")
            for path, text in asm_texts.items():
                (live / path).write_text(text, encoding="utf-8")
            spec = live / link.file
            spec.write_text(
                writer(spec.read_text(encoding="utf-8"), plan.old_pieces, written),
                encoding="utf-8",
            )
            problems = _build_all(s, live, log)
            if problems:
                _restore(live, written)
                outcomes.append(Outcome(tu, "failed", "build: " + " | ".join(problems), plan.new))
                _record(conn, plan, "failed", None, ("build: " + " | ".join(problems))[:4000])
                continue
            _git(live, "add", "-A", "--", *written, link.file)
            msg = f"rematch: {tu} to C ({len(plan.new)} functions, {len(plan.in_c)} in C)"
            _git(live, "commit", "-q", "-m", msg)
            sha = _git(live, "rev-parse", "HEAD")
            _record(conn, plan, "committed", sha, msg)
            outcomes.append(Outcome(tu, "committed", f"{sha[:10]} {msg}", plan.new))
            log(f"{tu}: committed {sha[:10]}")
    return outcomes


# --- verifying the branch ----------------------------------------------------------------


def verify_branch(s: Session, log: Log) -> list[tuple[str, bool, str]]:
    """Build every rematch/live commit since the pin, oldest first, in a scratch worktree
    (incremental builds) and check every version's ROM hash."""
    live = live_worktree(s)
    scratch = live.with_name(live.name + "-verify")
    if not scratch.is_dir():
        _git(live, "worktree", "add", "--detach", str(scratch), s.settings.target_pin)
        copy = s.definition.build_info.worktree_copy if s.definition.build_info else []
        for pattern in copy:
            for item in sorted(live.glob(pattern)):
                dest = scratch / item.relative_to(live)
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.run(["cp", "-a", str(item), str(dest)], check=True, timeout=600)
    commits = _git(
        live, "rev-list", "--reverse", "--first-parent", f"{s.settings.target_pin}..rematch/live"
    )
    results: list[tuple[str, bool, str]] = []
    for sha in commits.split():
        _git(scratch, "checkout", "-q", "--detach", sha)
        problems = _build_all(s, scratch, log)
        results.append((sha, not problems, " | ".join(problems) or "all hashes match"))
        log(f"{sha[:10]}: {'ok' if not problems else 'FAIL'}")
    return results
