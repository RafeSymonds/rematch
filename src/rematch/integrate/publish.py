"""`rematch publish`: merge rematch/live into a branch of the target and push it (SPEC §10.1).

The merge happens in the live worktree, in the direction branch -> rematch/live, so the merged
tree can be built and hash-checked before the branch moves. The branch is then fast-forwarded to
rematch/live: in the worktree that has it checked out (git refuses if that would overwrite local
changes), or by a compare-and-swap ref update when no worktree has it.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rematch.session import Session
from rematch.target.definition import TARGET_DIR_NAME

Log = Callable[[str], None]
LIVE_BRANCH = "rematch/live"
PUSH_TIMEOUT_SEC = 600


class PublishError(Exception):
    pass


@dataclass
class PublishResult:
    branch: str
    head: str  # the branch's commit after publishing (dry run: rematch/live's head)
    merged: bool  # a merge commit was made on rematch/live
    built: bool  # the merged tree was built and hash-checked
    pushed: str | None  # remote, when pushed


def _git(repo: Path, *args: str, timeout: float = 120) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout
    )
    if out.returncode != 0:
        raise PublishError(f"git {' '.join(args)}: {(out.stderr or out.stdout).strip()}")
    return out.stdout.strip()


def _ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True).returncode == 0


def checked_out_in(repo: Path, branch: str) -> Path | None:
    """The worktree that has `branch` checked out, if any."""
    current: Path | None = None
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line == f"branch refs/heads/{branch}":
            return current
    return None


def publish(
    live: Path,
    branch: str,
    remote: str,
    build: Callable[[], list[str]],
    log: Log,
    dry_run: bool = False,
) -> PublishResult:
    """Merge rematch/live into `branch` and push `branch` to `remote` (empty: no push).

    `build` builds every version in the live worktree and returns problems (empty = all ROM
    hashes match). It runs only when the merge brings in changes outside the target's
    `.rematch/` directory, since every rematch/live commit was already built and checked.
    """
    if not _ok(live, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
        raise PublishError(f"branch {branch!r} does not exist")
    old = _git(live, "rev-parse", f"refs/heads/{branch}")
    before = _git(live, "rev-parse", "HEAD")
    merged = built = False
    if not _ok(live, "merge-base", "--is-ancestor", old, before):
        incoming = [
            p
            for p in _git(live, "diff", "--name-only", f"{before}...{old}").splitlines()
            if not p.startswith(f"{TARGET_DIR_NAME}/")
        ]
        log(f"merging {branch} into {LIVE_BRANCH} ({len(incoming)} build file(s) changed)")
        if dry_run:
            return PublishResult(branch, before, True, bool(incoming), None)
        try:
            _git(live, "merge", "-q", "--no-edit", "-m", f"Merge {branch} into {LIVE_BRANCH}", old)
        except PublishError as exc:
            if _ok(live, "rev-parse", "-q", "--verify", "MERGE_HEAD"):
                _git(live, "merge", "--abort")
            raise PublishError(f"merging {branch} into {LIVE_BRANCH} failed: {exc}") from exc
        merged = True
        if incoming:
            problems = build()
            built = True
            if problems:
                _git(live, "reset", "-q", "--hard", before)
                raise PublishError("merged tree fails the build: " + " | ".join(problems))
    elif dry_run:
        return PublishResult(branch, before, False, False, None)
    head = _git(live, "rev-parse", "HEAD")
    if head != old:
        where = checked_out_in(live, branch)
        if where is not None:
            _git(where, "merge", "-q", "--ff-only", head)
        else:
            _git(live, "update-ref", f"refs/heads/{branch}", head, old)
        log(f"{branch}: {old[:10]} -> {head[:10]}")
    pushed = None
    if remote:
        _git(live, "push", "-q", remote, f"refs/heads/{branch}", timeout=PUSH_TIMEOUT_SEC)
        pushed = remote
        log(f"pushed {branch} to {remote}")
    return PublishResult(branch, head, merged, built, pushed)


def publish_live(
    s: Session,
    log: Log,
    branch: str | None = None,
    remote: str | None = None,
    dry_run: bool = False,
) -> PublishResult:
    """`publish` with the session's settings (`MERGE_BRANCH`, `PUSH_REMOTE`), holding the
    integration lock so no integration commits meanwhile."""
    from rematch.integrate.run import IntegrateError, _build_all, _lock, live_worktree

    branch = branch or s.settings.merge_branch
    if not branch:
        raise PublishError("no branch to merge into: set MERGE_BRANCH or pass --branch")
    try:
        with _lock(s.settings.rematch_var / "integrate.lock"):
            live = live_worktree(s)
            return publish(
                live,
                branch,
                s.settings.push_remote if remote is None else remote,
                lambda: _build_all(s, live, log),
                log,
                dry_run,
            )
    except IntegrateError as exc:
        raise PublishError(str(exc)) from exc
