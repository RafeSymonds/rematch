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
import hashlib
import json
import re
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from rematch.context.header_index import HeaderIndex
from rematch.context.protos import ProtosError
from rematch.context.protos import extract as extract_protos
from rematch.db import store
from rematch.diff.match import function_view
from rematch.doctor import sha1_file
from rematch.integrate.compose import compose_piece
from rematch.integrate.headers import committed_headers, compile_spec, header_edits, with_edits
from rematch.integrate.linkspec import WRITERS
from rematch.integrate.piece import PieceSpec, check_source, provisional_count, save_targets
from rematch.integrate.pret_asm import ParsedTU, Piece, SplitError, parse_tu, plan_pieces
from rematch.integrate.pret_asm import render_asm_pieces as render_pret_pieces
from rematch.lint.readability import LINT_VERSION, lint, passes
from rematch.session import Session
from rematch.toolchain.compile_db import CompileDB
from rematch.worker.attempt import now
from rematch.worker.sandbox import FunctionRecord, _vanilla_lines, context_stub

Log = Callable[[str], None]
Writer = Callable[[str, list[str], list[str]], str]  # link-order text, old, new -> text


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


def relint_stale(conn: sqlite3.Connection) -> int:
    """Re-lint matches stored under an older LINT_VERSION, so a new rule doesn't strand them
    (`passes` requires the current version). No tokens."""
    rows = conn.execute("SELECT function_id, c_source, lint_json FROM matches").fetchall()
    stale = []
    for fid, source, lint_json in rows:
        try:
            version = json.loads(lint_json or "{}").get("version")
        except json.JSONDecodeError:
            version = None
        if version != LINT_VERSION:
            stale.append((lint(source or "").to_json(), fid))
    if stale:
        with conn:
            conn.executemany("UPDATE matches SET lint_json = ? WHERE function_id = ?", stale)
    return len(stale)


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


def stored_source(
    conn: sqlite3.Connection, tu: str, functions: list[str], kind: str = "functions"
) -> str | None:
    row = conn.execute(
        "SELECT source FROM piece_sources WHERE tu_path = ? AND functions_json = ? AND kind = ?"
        " ORDER BY id DESC LIMIT 1",
        (tu, json.dumps(functions), kind),
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
    headers = piece_headers(s, piece_overrides([sources[n] for n in piece.functions]))
    for vi, version in enumerate(s.target.versions()):
        db = CompileDB.load(
            s.definition.build_dir(s.worktree, version) / build.compile_commands, s.worktree
        )
        compile_specs[version] = compile_spec(s, db.c_template_for(tu), headers)
        offsets[version] = {}
        first: int | None = None
        for name in piece.functions:
            entry = db.entry(tu)
            vobj, lines = _vanilla_lines(s, entry.object_path, name, asm_text[name], entry)
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
        s.settings.piece_compile_timeout_sec,
        s.settings.max_checks,
    )
    spec.save(control)
    mode_wrap = s.definition.worker.mode_wrap
    wraps = {
        f.name: mode_wrap[f.mode]
        for f in parsed.functions
        if f.name in piece.functions and f.mode in mode_wrap
    }
    return spec, compose_piece(includes, [(n, sources[n]) for n in piece.functions], wraps)


def piece_overrides(sources: list[str]) -> dict[str, str]:
    """name -> prototype from the candidates' rematch:protos blocks (first one wins)."""
    out: dict[str, str] = {}
    for source in sources:
        try:
            for name, decl in extract_protos(source).items():
                out.setdefault(name, decl)
        except ProtosError:
            continue
    return out


def piece_headers(s: Session, overrides: dict[str, str]) -> Path | None:
    """rematch/live's committed headers, with `overrides` applied when they change any."""
    base = committed_headers(s)
    if base is None or not overrides:
        return base
    dirs = s.definition.build_info.header_dirs if s.definition.build_info else []
    return with_edits(base, header_edits(base, dirs, overrides))


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
            break
        actual = sha1_file(rom)
        if actual != s.target.expected_sha1(version):
            problems.append(f"{version}: sha1 {actual}, expected {s.target.expected_sha1(version)}")
        if problems:
            break  # one failing version is enough to reject the change
    return problems


def _restore(live: Path, written: list[str]) -> None:
    _git(live, "reset", "-q", "--hard", "HEAD")
    for rel in written:
        p = live / rel
        if p.is_file() and not _git(live, "ls-files", rel):
            p.unlink()


def _record(
    conn: sqlite3.Connection,
    plan: TUPlan,
    status: str,
    sha: str | None,
    detail: str,
    plan_hash: str | None = None,
) -> None:
    pieces = [p.path for p in plan.pieces] if status == "committed" else plan.old_pieces
    with conn:
        conn.execute(
            "INSERT INTO integrations (tu_path, status, functions_json, pieces_json, commit_sha,"
            " detail, created_at, plan_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan.tu_path,
                status,
                json.dumps(plan.in_c),
                json.dumps(pieces),
                sha,
                detail,
                now(),
                plan_hash,
            ),
        )
        if status == "committed" and plan.new:
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


def _record_error(conn: sqlite3.Connection, tu: str, detail: str) -> None:
    pieces = current_pieces(conn, tu)
    with conn:
        conn.execute(
            "INSERT INTO integrations (tu_path, status, functions_json, pieces_json, detail,"
            " created_at) VALUES (?, 'failed', '[]', ?, ?, ?)",
            (tu, json.dumps(pieces), detail[:4000], now()),
        )


@dataclass
class Outcome:
    tu: str
    status: str  # committed | failed | planned | skipped
    detail: str
    new: list[str] = field(default_factory=list)


@dataclass
class PreparedTU:
    """A checked TU step, ready to write: nothing is in the live worktree yet."""

    plan: TUPlan
    files: dict[str, str]  # path -> text, every piece this step writes
    order: list[str]  # the TU's pieces in link order after this step
    plan_hash: str
    message: str  # `<tu> to C (N functions, M in C)`
    overrides: dict[str, str] = field(default_factory=dict)  # name -> prototype, for headers


def plan_hash(conn: sqlite3.Connection, plan: TUPlan, sources: dict[str, str]) -> str:
    """Hash of a step's inputs: the functions in C, their sources, stored piece sources and
    the current pieces. Equal hashes mean the step would be rebuilt exactly as before."""
    stored = conn.execute(
        "SELECT MAX(id) FROM piece_sources WHERE tu_path = ?", (plan.tu_path,)
    ).fetchone()[0]
    blob = json.dumps(
        [plan.in_c, [sources.get(n) for n in plan.in_c], stored, plan.old_pieces],
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def failed_before(conn: sqlite3.Connection, tu: str, h: str) -> bool:
    """True if a step with these exact inputs already failed for the TU. Any earlier row
    counts, not only the latest: the types and data steps of one TU alternate, so the latest
    row is often the other step's."""
    row = conn.execute(
        "SELECT 1 FROM integrations WHERE tu_path = ? AND status = 'failed' AND plan_hash = ?"
        " LIMIT 1",
        (tu, h),
    ).fetchone()
    return row is not None


def prepare_tu(
    s: Session,
    conn: sqlite3.Connection,
    tu: str,
    *,
    record: bool,
    fix: Callable[[str, PieceCheck, dict[str, str]], bool] | None = None,
) -> PreparedTU | Outcome:
    """Plan and check one TU's next step. Writes nothing to the live worktree."""
    tu_text = (s.worktree / tu).read_text(encoding="utf-8", errors="replace")
    try:
        parsed = parse_tu(tu_text)
    except SplitError as exc:
        return Outcome(tu, "skipped", f"cannot split: {exc}")
    plan = plan_tu(s, conn, tu, parsed)
    if plan is None:
        return Outcome(tu, "skipped", "no run ready")
    sources = dict(
        conn.execute(
            "SELECT f.name, m.c_source FROM matches m JOIN functions f"
            " ON f.id = m.function_id WHERE f.tu_path = ?",
            (tu,),
        ).fetchall()
    )
    h = plan_hash(conn, plan, sources)
    if failed_before(conn, tu, h):
        return Outcome(tu, "skipped", "unchanged since its last failure", plan.new)
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
        if record:
            _record(conn, plan, "failed", None, f"piece check: {detail}"[:4000], h)
        return Outcome(tu, "failed", f"piece check: {detail}", plan.new)
    c_undefined = set().union(*(c.undefined for c in checks))
    files = {c.piece.path: c.source for c in checks}
    files.update(render_pret_pieces(parsed, plan.pieces, c_undefined))
    message = f"{tu} to C ({len(plan.new)} functions, {len(plan.in_c)} in C)"
    overrides = piece_overrides([sources[n] for n in plan.in_c if n in sources])
    return PreparedTU(plan, files, [p.path for p in plan.pieces], h, message, overrides)


DataFix = Callable[[str, list[str], str, str, dict[str, str]], bool]
"""`fix_data(tu, functions, asm_piece, c_piece, asm_by_function)` runs a tu_data attempt; True
if it stored a passing source."""


def converted_tus(conn: sqlite3.Connection) -> list[str]:
    """Assembly TUs whose functions are all integrated."""
    rows = conn.execute(
        "SELECT tu_path FROM functions WHERE source_kind = 'asm' GROUP BY tu_path"
        " HAVING SUM(status != 'integrated') = 0 ORDER BY tu_path"
    ).fetchall()
    return [str(r[0]) for r in rows]


def data_ready_tus(conn: sqlite3.Connection, s: Session | None = None) -> list[str]:
    """Fully converted TUs that still have an assembly piece, plus (with `s`) assembly TUs
    that hold only data."""
    out = [
        tu for tu in converted_tus(conn) if any(p.endswith(".s") for p in current_pieces(conn, tu))
    ]
    return out + (data_only_tus(s, conn) if s is not None else [])


def data_only_tus(s: Session, conn: sqlite3.Connection) -> list[str]:
    """Assembly TUs in the link order with no functions, still assembly on rematch/live."""
    from fnmatch import fnmatch

    from rematch.inventory import link_order as link_order_parsers

    link = s.definition.require(s.definition.link_order, "link_order")
    tus = link_order_parsers.PARSERS[link.format](
        (s.worktree / link.file).read_text(encoding="utf-8")
    )
    with_functions = {str(r[0]) for r in conn.execute("SELECT DISTINCT tu_path FROM functions")}
    exclude = s.definition.source.exclude if s.definition.source else []
    live = s.settings.live_worktree
    return [
        tu.path
        for tu in tus
        if tu.path.endswith(".s")
        and tu.path not in with_functions
        and not any(fnmatch(tu.path, g) for g in exclude)
        and current_pieces(conn, tu.path) == [tu.path]
        and live is not None
        and (live / tu.path).is_file()
    ]


def prepare_data_tu(
    s: Session,
    conn: sqlite3.Connection,
    tu: str,
    *,
    record: bool,
    fix_data: DataFix | None = None,
) -> PreparedTU | Outcome:
    """The `tu_data` step for a fully converted TU: one C file with functions and data
    replaces the data-only assembly piece and the C file (SPEC §10)."""
    from rematch.integrate.data_check import check_data, prepare_data
    from rematch.integrate.tu_data import data_piece_text

    live = s.settings.live_worktree
    if live is None:
        raise IntegrateError("LIVE_WORKTREE is not set")
    pieces = current_pieces(conn, tu)
    parsed = parse_tu((s.worktree / tu).read_text(encoding="utf-8", errors="replace"))
    functions = [f.name for f in parsed.functions]
    pair = data_piece_text(live, pieces)
    if pair is None and not functions and pieces == [tu]:
        pair = (tu, f"src/{Path(tu).stem}.c")  # a data-only TU becomes a new C file
    if pair is None:
        return Outcome(tu, "skipped", "not one data piece and one C file")
    asm_piece, c_piece = pair
    plan = TUPlan(tu, functions, [], [Piece("c", c_piece, functions)], pieces)

    def inputs_hash(source: str | None) -> str:
        blob = json.dumps(["data", functions, source, pieces])
        return hashlib.sha256(blob.encode()).hexdigest()

    source = stored_source(conn, tu, functions, "data")
    if failed_before(conn, tu, inputs_hash(source)):
        return Outcome(tu, "skipped", "unchanged since its last failure")
    if source is None:
        if fix_data is None:
            return Outcome(tu, "skipped", "no data source yet (run with --fix)")
        asm = {f.name: f.text for f in parsed.functions}
        if fix_data(tu, functions, asm_piece, c_piece, asm):
            source = stored_source(conn, tu, functions, "data")
        if source is None:
            if record:
                _record(conn, plan, "failed", None, "tu_data: no passing source", inputs_hash(None))
            return Outcome(tu, "failed", "tu_data: no passing source")
    control = s.settings.rematch_workdir.expanduser() / "integrate_data" / tu.replace("/", "_")
    spec = prepare_data(s, tu, functions, control)
    result = check_data(spec, source, control / "build")
    if not result.ok:
        detail = "data check: " + " | ".join(result.report().splitlines()[:6])
        if record:
            _record(conn, plan, "failed", None, detail[:4000], inputs_hash(source))
        return Outcome(tu, "failed", detail)
    message = f"{tu} data to C ({asm_piece} removed)"
    return PreparedTU(plan, {c_piece: source}, [c_piece], inputs_hash(source), message)


def prepare_types_tu(
    s: Session,
    conn: sqlite3.Connection,
    tu: str,
    *,
    record: bool,
    fix: Callable[[str, PieceCheck, dict[str, str]], bool] | None = None,
) -> PreparedTU | Outcome:
    """The types step for a fully converted TU (SPEC §16.2): each C piece with two or more
    provisional typedefs gets a `type_merge` attempt; a source with fewer typedefs whose
    functions all still match replaces the piece."""
    live = s.settings.live_worktree
    if live is None:
        raise IntegrateError("LIVE_WORKTREE is not set")
    tu_text = (s.worktree / tu).read_text(encoding="utf-8", errors="replace")
    parsed = parse_tu(tu_text)
    info = {n: st for n, st, _ in _tu_functions(conn, tu)}
    done = [f.name for f in parsed.functions if info.get(f.name) == "integrated"]
    if len(done) != len(parsed.functions):
        return Outcome(tu, "skipped", "not fully converted")
    pieces = current_pieces(conn, tu)
    planned = plan_pieces(tu, parsed, set(done))
    if [p.path for p in planned] != pieces:
        return Outcome(tu, "skipped", "pieces are not the function pieces (data already in C?)")
    texts = {p.path: (live / p.path).read_text(encoding="utf-8") for p in planned if p.kind == "c"}
    h = hashlib.sha256(json.dumps(["types", pieces, texts], sort_keys=True).encode()).hexdigest()
    if failed_before(conn, tu, h):
        return Outcome(tu, "skipped", "unchanged since its last failure")
    sources = dict(
        conn.execute(
            "SELECT f.name, m.c_source FROM matches m JOIN functions f"
            " ON f.id = m.function_id WHERE f.tu_path = ?",
            (tu,),
        ).fetchall()
    )
    work = s.settings.rematch_workdir.expanduser() / "integrate_types" / tu.replace("/", "_")
    asm = {f.name: f.text for f in parsed.functions}
    files: dict[str, str] = {}
    tried = merged = False
    for piece in planned:
        if piece.kind != "c" or provisional_count(texts[piece.path]) < 2:
            continue
        source = stored_source(conn, tu, piece.functions, "types")
        if source == texts[piece.path]:  # this piece already carries its merged types
            merged = True
            continue
        tried = True
        before = provisional_count(texts[piece.path])
        control = work / Path(piece.path).stem / "control"
        spec, _ = prepare_piece(s, tu, tu_text, parsed, piece, sources, control)
        spec.types_below = before
        spec.save(control)
        if source is None and fix is not None:
            pending = PieceCheck(piece, False, [], set(), texts[piece.path], control)
            if fix(tu, pending, asm):
                source = stored_source(conn, tu, piece.functions, "types")
        if source is None or provisional_count(source) >= before:
            continue
        if check_source(spec, control, source, control.parent / "verify").ok:
            files[piece.path] = source
    plan = TUPlan(tu, done, [], planned, pieces)
    if not files:
        if not tried:
            why = "types already merged" if merged else "no piece with duplicate provisional types"
            return Outcome(tu, "skipped", why)
        if record:
            _record(conn, plan, "failed", None, "types: no merged source", h)
        return Outcome(tu, "failed", "types: no merged source")
    counts = ", ".join(
        f"{p}: {provisional_count(texts[p])}->{provisional_count(t)}" for p, t in files.items()
    )
    return PreparedTU(plan, files, pieces, h, f"{tu} provisional types merged ({counts})")


# --- landing batches -------------------------------------------------------------------

BATCH_START, BATCH_MAX = 8, 32
_batch = {"size": BATCH_START}  # adapts within a process: doubles on a pass, halves on a fail


def _journal(s: Session) -> Path:
    return s.settings.rematch_var / "integrate.journal"


def recover_journal(s: Session, log: Log) -> None:
    """Undo a batch that a killed process left written but not committed."""
    path = _journal(s)
    live = s.settings.live_worktree
    if not path.is_file() or live is None:
        return
    written = json.loads(path.read_text(encoding="utf-8"))
    if _git(live, "rev-parse", "--abbrev-ref", "HEAD") == "rematch/live":
        _restore(live, written)
        log(f"restored {len(written)} path(s) left by an interrupted integration")
    path.unlink()


def _header_writes(s: Session, live: Path, batch: list[PreparedTU]) -> dict[str, str]:
    """Header path -> text with the batch's prototype overrides applied to the live tree."""
    overrides: dict[str, str] = {}
    for p in batch:
        overrides.update(p.overrides)
    if not overrides or s.definition.build_info is None:
        return {}
    return header_edits(live, s.definition.build_info.header_dirs, overrides)


def _apply(
    s: Session, live: Path, link_file: str, writer: Writer, batch: list[PreparedTU]
) -> tuple[list[str], list[str]]:
    """Write the batch. Returns (every path written or removed, the paths to `git add`)."""
    headers = _header_writes(s, live, batch)
    written = sorted(
        {f for p in batch for f in p.files}
        | {o for p in batch for o in p.plan.old_pieces}
        | set(headers)
    )
    _journal(s).write_text(json.dumps(written), encoding="utf-8")
    spec = live / link_file
    text = spec.read_text(encoding="utf-8")
    for p in batch:
        for old in p.plan.old_pieces:
            if old not in p.order:  # a piece the step's link order no longer has
                _git(live, "rm", "-q", old)
        for path, content in p.files.items():
            (live / path).write_text(content, encoding="utf-8")
        text = writer(text, p.plan.old_pieces, p.order)
    spec.write_text(text, encoding="utf-8")
    for path, content in headers.items():
        (live / path).write_text(content, encoding="utf-8")
    added = sorted({f for p in batch for f in p.files} | set(headers) | {link_file})
    return written, added


def culprit(problems: list[str], batch: list[PreparedTU]) -> PreparedTU | None:
    """The one TU whose piece file names appear in the build errors, if exactly one does."""
    text = "\n".join(problems)
    hits = [
        p
        for p in batch
        if any(
            re.search(rf"(?<![A-Za-z0-9]){re.escape(Path(f).name)}(?![A-Za-z0-9_])", text)
            for f in p.files
        )
    ]
    return hits[0] if len(hits) == 1 else None


def _land(
    s: Session,
    conn: sqlite3.Connection,
    live: Path,
    link_file: str,
    writer: Writer,
    batch: list[PreparedTU],
    out: list[Outcome],
    log: Log,
) -> bool:
    """Write, build once, commit. On a failed build, find the culprit by name or by halves;
    each passing part is committed before the next part is written. True if the whole batch
    landed in one build."""
    written, added = _apply(s, live, link_file, writer, batch)
    try:
        problems = _build_all(s, live, log)
        if not problems:
            # removed pieces are already staged by `git rm`
            _git(live, "add", "-A", "--", *added)
            if len(batch) == 1:
                _git(live, "commit", "-q", "-m", f"rematch: {batch[0].message}")
            else:
                n = sum(len(p.plan.new) for p in batch)
                title = f"rematch: integrate {len(batch)} TUs ({n} functions)"
                body = "\n".join(p.message for p in batch)
                _git(live, "commit", "-q", "-m", title, "-m", body)
            sha = _git(live, "rev-parse", "HEAD")
            _journal(s).unlink(missing_ok=True)
    except BaseException:
        _restore(live, written)
        _journal(s).unlink(missing_ok=True)
        raise
    if not problems:
        for p in batch:
            _record(conn, p.plan, "committed", sha, p.message, p.plan_hash)
            out.append(Outcome(p.plan.tu_path, "committed", f"{sha[:10]} {p.message}", p.plan.new))
        log(f"committed {sha[:10]}: {len(batch)} TU(s)")
        return True
    _restore(live, written)
    _journal(s).unlink(missing_ok=True)
    detail = "build: " + " | ".join(problems)
    if len(batch) == 1:
        p = batch[0]
        _record(conn, p.plan, "failed", None, detail[:4000], p.plan_hash)
        out.append(Outcome(p.plan.tu_path, "failed", detail, p.plan.new))
        return False
    named = culprit(problems, batch)
    if named is not None:
        _record(
            conn,
            named.plan,
            "failed",
            None,
            (detail + " (named in the build output)")[:4000],
            named.plan_hash,
        )
        out.append(Outcome(named.plan.tu_path, "failed", detail, named.plan.new))
        log(f"{named.plan.tu_path}: named in the build output; retrying the rest")
        _land(s, conn, live, link_file, writer, [p for p in batch if p is not named], out, log)
        return False
    mid = len(batch) // 2
    log(f"batch of {len(batch)} failed; bisecting")
    _land(s, conn, live, link_file, writer, batch[:mid], out, log)
    _land(s, conn, live, link_file, writer, batch[mid:], out, log)
    return False


def integrate(
    s: Session,
    conn: sqlite3.Connection,
    tus: list[str],
    *,
    dry_run: bool,
    log: Log,
    fix: Callable[[str, PieceCheck, dict[str, str]], bool] | None = None,
    step: str = "functions",
    fix_data: DataFix | None = None,
    steps: list[tuple[str, list[str]]] | None = None,
) -> list[Outcome]:
    """Prepare every TU (plan, piece check, optional `fix`), then land the prepared steps in
    batches, one build per batch (SPEC §10). `step` is "functions", "types" (merge
    provisional types in fully converted TUs), "data" (`tu_data`) or "finish" (types, then
    data when the types step has nothing to land). `steps` runs several (step, TUs) jobs in
    one pool, in order, landing them in shared batches: a TU must appear in only one of them.
    `fix(tu, failed_check, asm_by_function)` runs a tu_fix attempt; True if it stored a
    passing source, after which the TU is checked again."""
    if s.definition.asm is None or s.definition.asm.format != "pret-macros":
        raise IntegrateError("integration needs [asm] format = 'pret-macros'")
    link = s.definition.require(s.definition.link_order, "link_order")
    writer = WRITERS.get(link.format)
    if writer is None:
        raise IntegrateError(f"no link-order writer for {link.format!r}")
    outcomes: list[Outcome] = []
    relint_stale(conn)
    with _lock(s.settings.rematch_var / "integrate.lock"):
        live = None
        if not dry_run:
            recover_journal(s, log)
            live = live_worktree(s)
        prepared: list[PreparedTU] = []

        def prepare(kind: str, tu: str) -> PreparedTU | Outcome | Exception:
            # each preparing thread has its own connection; landing stays on this thread
            pconn = store.connect(s.db_path)
            record = not dry_run
            try:
                if kind == "finish":
                    r = prepare_types_tu(s, pconn, tu, record=record, fix=fix)
                    if isinstance(r, PreparedTU):
                        return r  # the data step follows next cycle, on the merged types
                    d = prepare_data_tu(s, pconn, tu, record=record, fix_data=fix_data)
                    return r if isinstance(d, Outcome) and d.status == "skipped" else d
                if kind == "data":
                    return prepare_data_tu(s, pconn, tu, record=record, fix_data=fix_data)
                if kind == "types":
                    return prepare_types_tu(s, pconn, tu, record=record, fix=fix)
                return prepare_tu(s, pconn, tu, record=record, fix=fix)
            except Exception as exc:  # one TU's surprise must not stop the others
                return exc
            finally:
                pconn.close()

        jobs = [(kind, tu) for kind, kind_tus in (steps or [(step, tus)]) for tu in kind_tus]
        pool = ThreadPoolExecutor(max(1, s.settings.integrate_workers))
        try:
            futures = {pool.submit(prepare, kind, tu): tu for kind, tu in jobs}
            for future in as_completed(futures):
                tu, r = futures[future], future.result()
                if isinstance(r, Exception):
                    outcomes.append(Outcome(tu, "failed", f"prepare: {r}"))
                    if not dry_run:
                        _record_error(conn, tu, f"prepare: {r}")
                    continue
                if isinstance(r, Outcome):
                    outcomes.append(r)
                elif live is None:
                    desc = ", ".join(f"{path} ({len(r.files.get(path, ''))} B)" for path in r.order)
                    outcomes.append(Outcome(tu, "planned", desc, r.plan.new))
                else:
                    prepared.append(r)
                # land as soon as a batch is ready, so long cycles commit as they go
                if live is not None and len(prepared) >= _batch["size"]:
                    prepared = _land_batch(
                        s, conn, live, link.file, writer, prepared, outcomes, log
                    )
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        while live is not None and prepared:
            prepared = _land_batch(s, conn, live, link.file, writer, prepared, outcomes, log)
    return outcomes


def _land_batch(
    s: Session,
    conn: sqlite3.Connection,
    live: Path,
    link_file: str,
    writer: Writer,
    prepared: list[PreparedTU],
    outcomes: list[Outcome],
    log: Log,
) -> list[PreparedTU]:
    """Land the first batch of `prepared`, adapt the batch size, return the rest."""
    size = _batch["size"]
    batch, rest = prepared[:size], prepared[size:]
    whole = _land(s, conn, live, link_file, writer, batch, outcomes, log)
    _batch["size"] = min(BATCH_MAX, size * 2) if whole else max(1, size // 2)
    return rest


# --- verifying the branch ----------------------------------------------------------------


def verify_branch(s: Session, log: Log, since: str | None = None) -> list[tuple[str, bool, str]]:
    """Build every rematch/live commit after `since` (default: the last commit this verified,
    else the pin), oldest first, in a scratch worktree (incremental builds), and check every
    version's ROM hash. The last passing commit is remembered in REMATCH_VAR/verified."""
    marker = s.settings.rematch_var / "verified"
    if since is None:
        since = marker.read_text().strip() if marker.is_file() else s.settings.target_pin
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
    commits = _git(live, "rev-list", "--reverse", "--first-parent", f"{since}..rematch/live")
    results: list[tuple[str, bool, str]] = []
    for sha in commits.split():
        _git(scratch, "checkout", "-q", "--detach", sha)
        problems = _build_all(s, scratch, log)
        results.append((sha, not problems, " | ".join(problems) or "all hashes match"))
        log(f"{sha[:10]}: {'ok' if not problems else 'FAIL'}")
        if problems:
            break  # later commits build on this one; fix it first
        marker.write_text(sha, encoding="utf-8")
    return results
