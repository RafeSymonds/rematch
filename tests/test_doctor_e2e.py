"""`rematch doctor` end to end against a throwaway target repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rematch.cli import app
from rematch.config import Settings
from rematch.doctor import CheckResult, iter_doctor, set_writable
from tests.helpers import FORBIDDEN, git, make_target_repo


def settings_for(tmp_path: Path, repo: Path, pin: str, **extra: str) -> Settings:
    return Settings(
        _env_file=None,
        target_repo=repo,
        target_pin=pin,
        rematch_workdir=tmp_path / "work",
        rematch_var=tmp_path / "var",
        **extra,  # type: ignore[arg-type]
    )


def run(settings: Settings) -> dict[str, CheckResult]:
    return {r.name: r for r in iter_doctor(settings)}


def failures(results: dict[str, CheckResult]) -> set[str]:
    return {name for name, r in results.items() if not r.ok}


def test_all_pass_and_builds_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, pin = make_target_repo(tmp_path)
    build_log = tmp_path / "builds.log"
    monkeypatch.setenv("BUILD_LOG", str(build_log))
    settings = settings_for(tmp_path, repo, pin)

    results = run(settings)
    assert failures(results) == set(), results
    assert list(results) == [
        "claude version",
        "workdir isolation",
        "target.toml",
        "push guard",
        "vanilla worktree",
        "toolchain exec",
        "toolchain sees workdir",
        "build alpha",
        "build beta",
        "required paths",
        "vanilla read-only",
        "objdump",
        "ctags",
        "database",
    ]
    vanilla = tmp_path / "demo-vanilla"
    assert git(vanilla, "rev-parse", "HEAD") == pin
    assert (tmp_path / "var" / "demo.sqlite").is_file()
    assert build_log.read_text().split() == ["alpha", "beta"]

    # Second run reuses the existing ROMs: no rebuild.
    assert failures(run(settings)) == set()
    assert build_log.read_text().split() == ["alpha", "beta"]


def test_toolchain_exec_prefix_is_used(tmp_path: Path) -> None:
    repo, pin = make_target_repo(tmp_path)
    build_log = tmp_path / "builds.log"
    settings = settings_for(tmp_path, repo, pin, toolchain_exec=f"env BUILD_LOG={build_log}")
    results = run(settings)
    assert failures(results) == set(), results
    assert build_log.read_text().split() == ["alpha", "beta"]


def test_broken_toolchain_exec_skips_builds(tmp_path: Path) -> None:
    repo, pin = make_target_repo(tmp_path)
    results = run(settings_for(tmp_path, repo, pin, toolchain_exec="/nonexistent/exec"))
    assert failures(results) == {"toolchain exec"}
    assert "build alpha" not in results


def test_tampered_rom_fails(tmp_path: Path) -> None:
    repo, pin = make_target_repo(tmp_path)
    settings = settings_for(tmp_path, repo, pin)
    run(settings)
    vanilla = tmp_path / "demo-vanilla"
    with pytest.raises(PermissionError):  # doctor left the answer key read-only
        (vanilla / "out" / "beta.bin").write_bytes(b"tampered")
    set_writable(vanilla, writable=True)
    (vanilla / "out" / "beta.bin").write_bytes(b"tampered")
    results = run(settings)
    assert failures(results) == {"build beta"}
    assert "expected" in results["build beta"].detail


def test_head_not_at_pin_fails(tmp_path: Path) -> None:
    repo, pin = make_target_repo(tmp_path)
    run(settings_for(tmp_path, repo, pin))
    (repo / "new.txt").write_text("x")
    git(repo, "add", "new.txt")
    git(repo, "commit", "-q", "-m", "new")
    new_pin = git(repo, "rev-parse", "HEAD")
    results = run(settings_for(tmp_path, repo, new_pin))
    assert failures(results) == {"vanilla worktree"}
    assert "build alpha" not in results


def test_unknown_pin_fails(tmp_path: Path) -> None:
    repo, _ = make_target_repo(tmp_path)
    results = run(settings_for(tmp_path, repo, "0" * 40))
    assert failures(results) == {"vanilla worktree"}


def test_dirty_vanilla_worktree_fails(tmp_path: Path) -> None:
    repo, pin = make_target_repo(tmp_path)
    settings = settings_for(tmp_path, repo, pin)
    run(settings)
    set_writable(tmp_path / "demo-vanilla", writable=True)
    (tmp_path / "demo-vanilla" / "build.sh").write_text("echo edited\n")
    assert failures(run(settings)) == {"vanilla worktree"}


def test_forbidden_push_url_fails(tmp_path: Path) -> None:
    repo, pin = make_target_repo(tmp_path)
    git(repo, "remote", "set-url", "--push", "upstream", f"git@{FORBIDDEN}demo.git")
    results = run(settings_for(tmp_path, repo, pin))
    assert failures(results) == {"push guard"}
    assert "upstream" in results["push guard"].detail


def test_missing_hook_fails(tmp_path: Path) -> None:
    repo, pin = make_target_repo(tmp_path, install_hook=False)
    results = run(settings_for(tmp_path, repo, pin))
    assert failures(results) == {"push guard"}
    assert "not installed" in results["push guard"].detail


def test_modified_hook_fails(tmp_path: Path) -> None:
    repo, pin = make_target_repo(tmp_path)
    (repo / ".git" / "hooks" / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    assert failures(run(settings_for(tmp_path, repo, pin))) == {"push guard"}


def test_installed_hook_blocks_forbidden_push(tmp_path: Path) -> None:
    repo, _ = make_target_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-push"
    blocked = subprocess.run([str(hook), "upstream", f"https://{FORBIDDEN}demo.git"], check=False)
    allowed = subprocess.run([str(hook), "origin", "https://example.invalid/mine.git"], check=False)
    assert blocked.returncode != 0
    assert allowed.returncode == 0


def test_invalid_target_toml_stops_early(tmp_path: Path) -> None:
    repo, pin = make_target_repo(tmp_path)
    (repo / ".rematch" / "target.toml").write_text('name = "demo"\n')
    results = run(settings_for(tmp_path, repo, pin))
    assert failures(results) == {"target.toml"}
    assert list(results) == ["claude version", "workdir isolation", "target.toml"]


def test_workdir_under_repo_fails(tmp_path: Path) -> None:
    repo, pin = make_target_repo(tmp_path)
    (repo / "CLAUDE.md").write_text("x")
    settings = settings_for(tmp_path, repo, pin)
    settings = settings.model_copy(update={"rematch_workdir": repo / "sandboxes"})
    assert "workdir isolation" in failures(run(settings))


def test_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, pin = make_target_repo(tmp_path)
    monkeypatch.setenv("TARGET_REPO", str(repo))
    monkeypatch.setenv("TARGET_PIN", pin)
    monkeypatch.setenv("REMATCH_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv("REMATCH_VAR", str(tmp_path / "var"))
    cli = CliRunner()
    ok = cli.invoke(app, ["doctor"])
    assert ok.exit_code == 0, ok.output
    assert ok.output.count("PASS") == 14

    monkeypatch.delenv("TARGET_PIN")
    bad = cli.invoke(app, ["doctor"])
    assert bad.exit_code == 1
    assert "FAIL config" in bad.output
