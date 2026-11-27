"""Headers as rematch/live has them (SPEC §10, prototype overrides).

Integration may change a header: a rematch:protos block replaces a placeholder prototype.
Every compile that depends on headers (worker contexts, piece, types and data checks) then
reads the header directories named in target.toml `[build] header_dirs` from a copy of
rematch/live's committed tree, never from the working tree a batch may be writing. A piece
whose own overrides haven't landed yet compiles against a copy with its edits applied.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from rematch.context.protos import replace_declarations
from rematch.session import Session
from rematch.toolchain.compile_db import CompileEntry
from rematch.worker.task import CompileSpec

LIVE_REF = "rematch/live"


def _dirs(s: Session) -> list[str]:
    build = s.definition.build_info
    return list(build.header_dirs) if build else []


def committed_headers(s: Session) -> Path | None:
    """A directory holding the header dirs of rematch/live's last commit (cached by tree), or
    None when the target names no header dirs or has no live worktree."""
    dirs, live = _dirs(s), s.settings.live_worktree
    if not dirs or live is None or not (live / ".git").exists():
        return None
    trees = []
    for d in dirs:
        out = subprocess.run(
            ["git", "-C", str(live), "rev-parse", f"{LIVE_REF}:{d}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if out.returncode != 0:
            return None
        trees.append(out.stdout.strip())
    key = hashlib.sha256("\n".join(trees).encode()).hexdigest()[:16]
    root = s.settings.rematch_workdir.expanduser() / "headers"
    dest = root / key
    if dest.is_dir():
        return dest
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=root, prefix=".tmp-"))
    archive = subprocess.run(
        ["git", "-C", str(live), "archive", "--format=tar", LIVE_REF, *dirs],
        capture_output=True,
        timeout=300,
        check=True,
    )
    tar_path = tmp / ".headers.tar"
    tar_path.write_bytes(archive.stdout)
    with tarfile.open(tar_path) as tar:
        tar.extractall(tmp, filter="data")
    tar_path.unlink()
    try:
        tmp.rename(dest)
    except OSError:  # another thread extracted the same tree first
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


def header_edits(base: Path, dirs: list[str], overrides: dict[str, str]) -> dict[str, str]:
    """Header path (relative to the repo) -> new text, for every header under `base` that
    declares an overridden function with a different prototype."""
    edits: dict[str, str] = {}
    if not overrides:
        return edits
    for d in dirs:
        for path in sorted((base / d).rglob("*.h")):
            rel = path.relative_to(base).as_posix()
            text = original = path.read_text(encoding="utf-8", errors="replace")
            for name, decl in overrides.items():
                if name in text:
                    text = replace_declarations(text, name, decl) or text
            if text != original:
                edits[rel] = text
    return edits


def with_edits(base: Path, edits: dict[str, str]) -> Path:
    """A copy of `base` with `edits` applied (cached by content)."""
    if not edits:
        return base
    key = hashlib.sha256(json.dumps([base.name, sorted(edits.items())]).encode()).hexdigest()[:16]
    dest = base.parent / f"{base.name}-{key}"
    if dest.is_dir():
        return dest
    tmp = Path(tempfile.mkdtemp(dir=base.parent, prefix=".tmp-"))
    shutil.copytree(base, tmp, dirs_exist_ok=True)
    for rel, text in edits.items():
        (tmp / rel).write_text(text, encoding="utf-8")
    try:
        tmp.rename(dest)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


def rewrite_includes(
    argv: list[str], directory: Path, worktree: Path, headers: Path, dirs: list[str]
) -> list[str]:
    """`argv` with every -I that resolves to `worktree/<dir>` pointed at `headers/<dir>`."""
    targets = {(worktree / d).resolve(): (headers / d) for d in dirs}
    out: list[str] = []
    take_next = False
    for arg in argv:
        if take_next:
            hit = targets.get((directory / arg).resolve())
            out.append(str(hit) if hit else arg)
            take_next = False
            continue
        if arg == "-I":
            take_next = True
            out.append(arg)
            continue
        if arg.startswith("-I") and len(arg) > 2:
            hit = targets.get((directory / arg[2:]).resolve())
            out.append(f"-I{hit}" if hit else arg)
            continue
        out.append(arg)
    return out


def compile_spec(s: Session, entry: CompileEntry, headers: Path | None) -> dict[str, object]:
    """The compile entry as a CompileSpec dict, its header -I flags pointed at `headers`."""
    spec = CompileSpec.of(entry)
    if headers is not None:
        argv = rewrite_includes(spec.argv, Path(spec.directory), s.worktree, headers, _dirs(s))
        spec = spec.model_copy(update={"argv": argv})
    return spec.model_dump()
