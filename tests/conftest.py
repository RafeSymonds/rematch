"""Test isolation: fake claude binary, no real .env, no user/system git config."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

FAKES = Path(__file__).parent / "fakes"
FAKE_CLAUDE = FAKES / "claude"

_SETTINGS_ENV = (
    "TARGET_REPO",
    "TARGET_PIN",
    "TARGET_VERSIONS",
    "TOOLCHAIN_EXEC",
    "BUILD_TIMEOUT_SEC",
    "REMATCH_DB",
    "REMATCH_VAR",
    "REMATCH_WORKDIR",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("CLAUDE_BIN", str(FAKE_CLAUDE))
    monkeypatch.setenv("OBJDUMP", str(FAKES / "objdump"))
    monkeypatch.setenv("CTAGS", str(FAKES / "ctags"))
    for key in _SETTINGS_ENV:
        monkeypatch.delenv(key, raising=False)
    # A real .env in the repo root must never leak into tests.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    yield
    # Trap 10: no test may point CLAUDE_BIN at a real binary.
    claude_bin = Path(os.environ["CLAUDE_BIN"])
    assert claude_bin.parent == FAKES or claude_bin.is_relative_to(tmp_path), (
        "CLAUDE_BIN must be a fake"
    )
