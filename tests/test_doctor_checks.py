from __future__ import annotations

from pathlib import Path

import pytest

from rematch import doctor
from tests.conftest import FAKE_CLAUDE


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("2.1.332 (Claude Code)", (2, 1, 332)),
        ("claude 10.0.1\n", (10, 0, 1)),
        ("no version here", None),
        ("", None),
    ],
)
def test_parse_claude_version(output: str, expected: tuple[int, int, int] | None) -> None:
    assert doctor.parse_claude_version(output) == expected


def test_claude_version_fake_passes() -> None:
    result = doctor.check_claude_version(str(FAKE_CLAUDE))
    assert result.ok, result.detail


@pytest.mark.parametrize(
    ("version", "ok"), [("2.1.259", True), ("2.1.258", False), ("1.9.999", False)]
)
def test_claude_version_threshold(monkeypatch: pytest.MonkeyPatch, version: str, ok: bool) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_VERSION", version)
    assert doctor.check_claude_version(str(FAKE_CLAUDE)).ok is ok


def test_claude_version_garbage_and_missing(tmp_path: Path) -> None:
    garbage = tmp_path / "claude"
    garbage.write_text("#!/bin/sh\necho hello\n")
    garbage.chmod(0o755)
    assert not doctor.check_claude_version(str(garbage)).ok
    assert not doctor.check_claude_version(str(tmp_path / "nope")).ok


@pytest.mark.parametrize("name", ["CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude"])
@pytest.mark.parametrize("level", ["work", "a", "root"])
def test_agent_files_flagged_at_any_level(tmp_path: Path, name: str, level: str) -> None:
    root = tmp_path / "root"
    workdir = root / "a" / "work"
    workdir.mkdir(parents=True)
    where = {"work": workdir, "a": root / "a", "root": root}[level]
    if name == ".claude":
        (where / name).mkdir()
    else:
        (where / name).write_text("x")
    hits = doctor.find_agent_files(workdir, exempt=tmp_path / "unrelated")
    assert hits == [(where / name).resolve()]
    assert not doctor.check_workdir(workdir, tmp_path / "unrelated").ok


def test_user_config_dir_is_exempt(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    workdir = home / ".rematch" / "work"
    assert doctor.find_agent_files(workdir, exempt=home / ".claude") == []
    assert doctor.check_workdir(workdir, home / ".claude").ok
    # The exemption covers only that directory, not a CLAUDE.md next to it.
    (home / "CLAUDE.md").write_text("x")
    assert doctor.find_agent_files(workdir, exempt=home / ".claude") == [
        (home / "CLAUDE.md").resolve()
    ]


def test_claude_config_dir_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert doctor.claude_config_dir() == tmp_path / "cfg"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert doctor.claude_config_dir() == Path.home() / ".claude"


def test_required_paths(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    assert doctor.check_required_paths(tmp_path, ["tools"]).ok
    result = doctor.check_required_paths(tmp_path, ["tools", "missing/bin"])
    assert not result.ok and "missing/bin" in result.detail
