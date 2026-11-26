from __future__ import annotations

from pathlib import Path

import pytest

from rematch.target.definition import (
    BuildError,
    TargetDefError,
    TomlTarget,
    load_target_def,
)
from rematch.toolchain.runner import ToolchainRunner
from tests.helpers import ROM_SHA1, TARGET_TOML, make_target_repo

SHA = "a" * 40


def write_toml(root: Path, body: str) -> Path:
    (root / ".rematch").mkdir(parents=True, exist_ok=True)
    (root / ".rematch" / "target.toml").write_text(body)
    return root


def minimal(**overrides: str) -> str:
    fields = {
        "name": '"demo"',
        "default_versions": '["us"]',
        "extra": "",
        "us": f'build = ["make"]\nrom = "out/us.bin"\nsha1 = "{SHA}"',
    }
    fields.update(overrides)
    return (
        f"name = {fields['name']}\ndefault_versions = {fields['default_versions']}\n"
        f"{fields['extra']}\n[versions.us]\n{fields['us']}\n"
    )


def test_valid_definition(tmp_path: Path) -> None:
    definition = load_target_def(write_toml(tmp_path, TARGET_TOML))
    assert definition.name == "demo"
    assert definition.default_versions == ["alpha", "beta"]
    assert definition.versions["alpha"].sha1 == ROM_SHA1
    assert definition.required_paths == ["build.sh"]


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(TargetDefError, match="not found"):
        load_target_def(tmp_path)


def test_bad_toml_syntax(tmp_path: Path) -> None:
    with pytest.raises(TargetDefError):
        load_target_def(write_toml(tmp_path, "name = "))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"default_versions": '["us", "jp"]'}, "without a \\[versions"),
        ({"us": 'build = ["make"]\nrom = "out/us.bin"\nsha1 = "ABC"'}, "sha1"),
        ({"us": f'build = []\nrom = "out/us.bin"\nsha1 = "{SHA}"'}, "build"),
        ({"us": f'build = ["make"]\nrom = "../us.bin"\nsha1 = "{SHA}"'}, "relative"),
        ({"us": f'build = ["make"]\nrom = "/abs/us.bin"\nsha1 = "{SHA}"'}, "relative"),
        ({"extra": 'required_paths = ["a/../../b"]'}, "relative"),
        ({"extra": 'forbidden_push_urls = [" "]'}, "non-empty"),
        ({"extra": 'typo_field = "x"'}, "typo_field"),
        ({"name": '"../evil"'}, "name"),
        ({"default_versions": "[]"}, "default_versions"),
    ],
)
def test_invalid_definitions(tmp_path: Path, overrides: dict[str, str], message: str) -> None:
    with pytest.raises(TargetDefError, match=message):
        load_target_def(write_toml(tmp_path, minimal(**overrides)))


def test_version_override(tmp_path: Path) -> None:
    definition = load_target_def(write_toml(tmp_path, TARGET_TOML))
    target = TomlTarget(definition, ToolchainRunner(), 10, ["beta"])
    assert target.versions() == ["beta"]
    with pytest.raises(TargetDefError, match="gamma"):
        TomlTarget(definition, ToolchainRunner(), 10, ["gamma"])


def test_build_runs_argv_in_worktree(tmp_path: Path) -> None:
    repo, _ = make_target_repo(tmp_path)
    target = TomlTarget(load_target_def(repo), ToolchainRunner(), 10)
    rom = target.build(repo, "alpha")
    assert rom == repo / "out" / "alpha.bin"
    assert rom.is_file()


def test_build_failure_reports_output(tmp_path: Path) -> None:
    repo = write_toml(
        tmp_path,
        minimal(us=f'build = ["sh", "-c", "echo boom >&2; exit 3"]\nrom = "x"\nsha1 = "{SHA}"'),
    )
    target = TomlTarget(load_target_def(repo), ToolchainRunner(), 10)
    with pytest.raises(BuildError, match=r"exited 3(.|\n)*boom"):
        target.build(repo, "us")
