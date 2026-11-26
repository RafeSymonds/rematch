from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from rematch.config import Settings


def test_required_keys_missing() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]
    missing = {err["loc"][0] for err in excinfo.value.errors()}
    assert missing == {"target_repo", "target_pin"}


def test_defaults_and_user_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TARGET_REPO", "~/somewhere/target")
    monkeypatch.setenv("TARGET_PIN", "abc123")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.target_repo == Path.home() / "somewhere" / "target"
    assert settings.rematch_workdir == Path.home() / ".rematch" / "work"
    assert settings.toolchain_exec == ""
    assert settings.build_timeout_sec == 3600
    assert settings.max_checks == 40
    assert settings.version_override() == []
    assert settings.db_path("demo") == Path("var") / "demo.sqlite"


def test_env_file_loading_and_empty_db(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "TARGET_REPO=/t\nTARGET_PIN=deadbeef\nREMATCH_DB=\nTARGET_VERSIONS= b , a ,\n"
        "TOOLCHAIN_EXEC=docker exec -i -w {cwd} box\n"
    )
    settings = Settings(_env_file=env)  # type: ignore[call-arg]
    assert settings.rematch_db is None
    assert settings.version_override() == ["b", "a"]
    assert settings.toolchain_exec == "docker exec -i -w {cwd} box"


def test_explicit_db_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMATCH_DB", "~/x.sqlite")
    settings = Settings(_env_file=None, target_repo=Path("/t"), target_pin="p")
    assert settings.db_path("demo") == Path.home() / "x.sqlite"


def test_api_key_is_secret() -> None:
    settings = Settings(
        _env_file=None, target_repo=Path("/t"), target_pin="p", anthropic_api_key="sk-1"
    )
    assert "sk-1" not in repr(settings)
