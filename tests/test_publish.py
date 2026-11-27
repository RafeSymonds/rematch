"""`rematch publish`: merge rematch/live into a branch and push it (SPEC §10.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rematch.integrate.publish import PublishError, checked_out_in, publish
from tests.helpers import git


def commit(repo: Path, rel: str, text: str, msg: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """A target repo on `main` and a live worktree on rematch/live with one integration."""
    repo = tmp_path / "target"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "src/a.c", "int a;\n", "init")
    commit(repo, ".rematch/target.toml", "name = 'x'\n", "definition")
    live = tmp_path / "live"
    git(repo, "worktree", "add", "-q", "-b", "rematch/live", str(live), "main")
    commit(live, "src/b.c", "int b;\n", "rematch: b to C")
    return repo, live


class Builds:
    def __init__(self, problems: list[str] | None = None) -> None:
        self.calls = 0
        self.problems = problems or []

    def __call__(self) -> list[str]:
        self.calls += 1
        return self.problems


def test_fast_forward_updates_checked_out_branch(repos: tuple[Path, Path]) -> None:
    repo, live = repos
    build = Builds()
    r = publish(live, "main", "", build, lambda _: None)
    assert (r.merged, r.built, r.pushed) == (False, False, None)
    assert git(repo, "rev-parse", "main") == git(live, "rev-parse", "HEAD") == r.head
    assert (repo / "src/b.c").read_text() == "int b;\n"  # the main worktree moved too
    assert build.calls == 0


def test_definition_only_changes_merge_without_build(repos: tuple[Path, Path]) -> None:
    repo, live = repos
    commit(repo, ".rematch/TARGET.md", "facts\n", "notes")
    build = Builds()
    r = publish(live, "main", "", build, lambda _: None)
    assert r.merged and not r.built and build.calls == 0
    assert git(repo, "rev-parse", "main") == git(live, "rev-parse", "HEAD")
    assert (live / ".rematch/TARGET.md").is_file()


def test_build_changes_are_built_before_the_branch_moves(repos: tuple[Path, Path]) -> None:
    repo, live = repos
    commit(repo, "src/c.c", "int c;\n", "human work")
    build = Builds()
    r = publish(live, "main", "", build, lambda _: None)
    assert r.merged and r.built and build.calls == 1
    assert git(repo, "rev-parse", "main") == r.head


def test_failed_build_leaves_both_branches(repos: tuple[Path, Path]) -> None:
    repo, live = repos
    main = commit(repo, "src/c.c", "int c;\n", "human work")
    before = git(live, "rev-parse", "HEAD")
    with pytest.raises(PublishError, match="fails the build"):
        publish(live, "main", "", Builds(["v1: sha1 mismatch"]), lambda _: None)
    assert git(live, "rev-parse", "HEAD") == before
    assert git(repo, "rev-parse", "main") == main
    assert not git(live, "status", "--porcelain")


def test_conflict_aborts_the_merge(repos: tuple[Path, Path]) -> None:
    repo, live = repos
    commit(repo, "src/b.c", "int human_b;\n", "human b")
    before = git(live, "rev-parse", "HEAD")
    with pytest.raises(PublishError, match="merging main"):
        publish(live, "main", "", Builds(), lambda _: None)
    assert git(live, "rev-parse", "HEAD") == before
    assert not git(live, "status", "--porcelain")


def test_branch_not_checked_out_and_push(repos: tuple[Path, Path], tmp_path: Path) -> None:
    repo, live = repos
    git(repo, "branch", "pub", "main")
    assert checked_out_in(live, "pub") is None
    assert checked_out_in(live, "main") == repo.resolve()
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    r = publish(live, "pub", "origin", Builds(), lambda _: None)
    assert r.pushed == "origin"
    assert git(remote, "rev-parse", "pub") == git(repo, "rev-parse", "pub") == r.head


def test_dry_run_changes_nothing(repos: tuple[Path, Path]) -> None:
    repo, live = repos
    main = commit(repo, "src/c.c", "int c;\n", "human work")
    build = Builds()
    r = publish(live, "main", "origin", build, lambda _: None, dry_run=True)
    assert r.merged and r.built and r.pushed is None and build.calls == 0
    assert git(repo, "rev-parse", "main") == main


def test_missing_branch(repos: tuple[Path, Path]) -> None:
    _, live = repos
    with pytest.raises(PublishError, match="does not exist"):
        publish(live, "nope", "", Builds(), lambda _: None)
