"""`rematch inventory --history`: when each human-C function left assembly (SPEC §11.1).

Only precision after the bench cutoff matters. A function is "decompiled after cutoff" if it is
still in assembly at the last first-parent commit before `BENCH_CUTOFF_DATE`. Its exact date is
found by bisecting the first-parent commits between that commit and the pin, grouping functions
that leave together. Functions already in C at the cutoff get no date (contaminated pool).

Presence is keyed by (module key, address): the overlay index for overlays (overlay names
change over time, indices don't) or the block name otherwise, and the address from the
assembly's address comments, which are in one version's addresses (`[asm] address_version`).
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from rematch.inventory import asm_parser, link_order, mapfile
from rematch.target.definition import TargetDef
from rematch.toolchain.compile_db import CompileDB, CompileDBError

Log = Callable[[str], None]
Key = tuple[str, int]  # (module key, address)


class HistoryError(Exception):
    pass


@dataclass(frozen=True)
class Commit:
    sha: str
    date: str  # committer date, ISO 8601


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=300, check=False
    )
    if result.returncode != 0:
        raise HistoryError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def commit_before(repo: Path, ref: str, date: str) -> Commit | None:
    # A bare date means "that day at the current time of day" to git; pin it to midnight UTC.
    before = f"{date}T00:00:00Z" if len(date) == 10 else date
    out = _git(repo, "rev-list", "--first-parent", "-1", f"--before={before}", "--format=%cI", ref)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    return Commit(lines[0].removeprefix("commit ").strip(), lines[1].strip())


def first_parent_range(repo: Path, start: str, end: str) -> list[Commit]:
    """Commits after `start` up to and including `end`, oldest first."""
    out = _git(repo, "rev-list", "--first-parent", "--reverse", "--format=%cI", f"{start}..{end}")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return [
        Commit(lines[i].removeprefix("commit ").strip(), lines[i + 1].strip())
        for i in range(0, len(lines), 2)
    ]


class TreeReader:
    """Reads files at commits through one `git cat-file --batch` process."""

    def __init__(self, repo: Path) -> None:
        self._repo = repo
        self._proc = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

    def close(self) -> None:
        if self._proc.stdin:
            self._proc.stdin.close()
        self._proc.wait(timeout=30)

    def __enter__(self) -> TreeReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def read(self, commit: str, path: str) -> str | None:
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._proc.stdin.write(f"{commit}:{path}\n".encode())
        self._proc.stdin.flush()
        header = self._proc.stdout.readline().decode().split()
        if len(header) < 3 or header[1] == "missing":
            return None
        size = int(header[2])
        data = self._proc.stdout.read(size)
        self._proc.stdout.read(1)  # trailing newline
        return data.decode("utf-8", errors="replace")

    def files(self, commit: str) -> set[str]:
        return set(_git(self._repo, "ls-tree", "-r", "--name-only", commit).splitlines())


def module_key(tu: link_order.LinkedTU) -> str:
    return f"overlay:{tu.overlay}" if tu.overlay is not None else tu.module


def resolve_object(path: str, files: set[str]) -> str | None:
    """Older link orders list objects (`asm/x.o`); map them to the source that exists."""
    p = PurePosixPath(path)
    if p.suffix in (".s", ".c"):
        return path if path in files else None
    for suffix in (".s", ".c"):
        candidate = str(p.with_suffix(suffix))
        if candidate in files:
            return candidate
    return None


@dataclass
class Presence:
    """(module key, address) of every assembly function at each commit, cached."""

    reader: TreeReader
    link_files: list[str]
    asm_format: str
    cache: dict[str, set[Key]] = field(default_factory=dict)

    def at(self, commit: str) -> set[Key]:
        if commit in self.cache:
            return self.cache[commit]
        files = self.reader.files(commit)
        text = next(
            (t for f in self.link_files if (t := self.reader.read(commit, f)) is not None), None
        )
        if text is None:
            raise HistoryError(f"no link-order file ({self.link_files}) at {commit[:12]}")
        keys: set[Key] = set()
        for tu in parse_link_order_lenient(text):
            source = resolve_object(tu.path, files)
            if source is None or not source.endswith(".s"):
                continue
            asm_text = self.reader.read(commit, source) or ""
            try:
                funcs = asm_parser.PARSERS[self.asm_format](asm_text)
            except asm_parser.AsmParseError:
                continue
            key = module_key(tu)
            keys.update((key, f.address) for f in funcs if f.address is not None)
        self.cache[commit] = keys
        return keys


_OBJECT_O = re.compile(r"^(\s*Object\s+\S+)\.o\b")


def parse_link_order_lenient(text: str) -> list[link_order.LinkedTU]:
    """nitro-lsf that also accepts `.o` objects (older trees list objects, not sources).

    `x.o` is read as `x.s`; `resolve_object` later keeps only paths that exist as assembly.
    """
    patched = "\n".join(_OBJECT_O.sub(r"\1.s", line) for line in text.splitlines())
    return link_order.parse_nitro_lsf(patched)


def decompiled_dates(
    presence: Presence,
    targets: dict[int, Key],
    cutoff: Commit,
    commits: list[Commit],
    log: Log,
) -> dict[int, Commit]:
    """First commit (in `commits`, after `cutoff`) where each target is no longer in assembly.

    Targets not in assembly at `cutoff` are left out. `commits[-1]` must be the pin, where every
    target is already in C.
    """
    at_cutoff = presence.at(cutoff.sha)
    pending = {fid for fid, key in targets.items() if key in at_cutoff}
    log(f"{len(pending)} functions were still in assembly at the cutoff commit {cutoff.sha[:12]}")
    timeline = [cutoff, *commits]
    out: dict[int, Commit] = {}

    def solve(lo: int, hi: int, fids: set[int]) -> None:
        # Invariant: every fid is present at timeline[lo] and absent at timeline[hi].
        if not fids:
            return
        if hi - lo == 1:
            for fid in fids:
                out[fid] = timeline[hi]
            return
        mid = (lo + hi) // 2
        here = presence.at(timeline[mid].sha)
        present = {f for f in fids if targets[f] in here}
        solve(lo, mid, fids - present)
        solve(mid, hi, present)

    if pending:
        final = presence.at(timeline[-1].sha)
        still = {f for f in pending if targets[f] in final}
        if still:
            log(f"{len(still)} functions are still in assembly at the pin; skipped")
        solve(0, len(timeline) - 1, pending - still)
    return out


@dataclass
class HistoryReport:
    cutoff_commit: str
    cutoff_date: str
    commits_after: int
    c_functions: int
    dated: int
    no_address: int
    commits_read: int


def run_history(
    repo: Path,
    pin: str,
    cutoff_date: str,
    link_files: list[str],
    asm_format: str,
    targets: dict[int, Key],
    log: Log,
) -> tuple[dict[int, Commit], HistoryReport]:
    cutoff = commit_before(repo, pin, cutoff_date)
    if cutoff is None:
        raise HistoryError(f"no first-parent commit before {cutoff_date}")
    commits = first_parent_range(repo, cutoff.sha, pin)
    if not commits:
        raise HistoryError(f"TARGET_PIN is not after the cutoff commit {cutoff.sha[:12]}")
    log(f"cutoff commit {cutoff.sha[:12]} ({cutoff.date}), {len(commits)} commits since")
    with TreeReader(repo) as reader:
        presence = Presence(reader, link_files, asm_format)
        dates = decompiled_dates(presence, targets, cutoff, commits, log)
        report = HistoryReport(
            cutoff_commit=cutoff.sha,
            cutoff_date=cutoff.date,
            commits_after=len(commits),
            c_functions=len(targets),
            dated=len(dates),
            no_address=0,
            commits_read=len(presence.cache),
        )
    return dates, report


def targets_from_db(
    definition: TargetDef, worktree: Path, conn: sqlite3.Connection, primary: str
) -> tuple[dict[int, Key], int]:
    """(module key, address in the comment version) for every c_human function.

    Returns the targets and the number of functions whose address could not be resolved.
    """
    order = definition.require(definition.link_order, "link_order")
    build = definition.require(definition.build_info, "build")
    asm = definition.require(definition.asm, "asm")
    version = asm.address_version or primary
    tus = link_order.PARSERS[order.format]((worktree / order.file).read_text(encoding="utf-8"))
    key_of_module = {tu.module: module_key(tu) for tu in tus}
    build_dir = definition.build_dir(worktree, version)
    db = CompileDB.load(build_dir / build.compile_commands, worktree)
    symbols = mapfile.PARSERS[build.map_format](
        (build_dir / build.map).read_text(encoding="utf-8", errors="replace")
    )
    address = {(s.object, s.name): s.address for s in symbols}
    targets: dict[int, Key] = {}
    missing = 0
    rows = conn.execute(
        "SELECT id, name, module, tu_path FROM functions WHERE source_kind = 'c_human'"
    ).fetchall()
    for fid, name, module, tu_path in rows:
        try:
            obj = db.entry(tu_path).object_path.name
        except CompileDBError:
            missing += 1
            continue
        addr = address.get((obj, name))
        if addr is None or module not in key_of_module:
            missing += 1
            continue
        targets[fid] = (key_of_module[module], addr)
    return targets, missing
