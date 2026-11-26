"""`.rematch/target.toml` model and loader (SPEC §2.1), plus the adapter built from it."""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from rematch.toolchain.runner import ToolchainRunner

TARGET_DIR_NAME = ".rematch"
TARGET_TOML = "target.toml"
PRE_PUSH_HOOK = "pre-push"


class TargetDefError(Exception):
    """target.toml is missing, unreadable or invalid."""


class BuildError(Exception):
    """A version's build command failed."""


def _check_relative(path: str) -> str:
    parts = PurePosixPath(path).parts
    if not path or PurePosixPath(path).is_absolute() or ".." in parts:
        raise ValueError(f"must be a relative path without '..': {path!r}")
    return path


class VersionDef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    build: list[str] = Field(min_length=1)
    rom: str
    sha1: str = Field(pattern=r"^[0-9a-f]{40}$")

    @field_validator("rom")
    @classmethod
    def _rom_relative(cls, value: str) -> str:
        return _check_relative(value)


class TargetDef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Used in file names (the default DB path), so keep it filesystem-safe.
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    default_versions: list[str] = Field(min_length=1)
    forbidden_push_urls: list[str] = []
    required_paths: list[str] = []
    versions: dict[str, VersionDef]

    @field_validator("required_paths")
    @classmethod
    def _paths_relative(cls, value: list[str]) -> list[str]:
        return [_check_relative(p) for p in value]

    @field_validator("forbidden_push_urls")
    @classmethod
    def _patterns_nonempty(cls, value: list[str]) -> list[str]:
        if any(not p.strip() for p in value):
            raise ValueError("forbidden_push_urls entries must be non-empty")
        return value

    @model_validator(mode="after")
    def _versions_defined(self) -> TargetDef:
        missing = [v for v in self.default_versions if v not in self.versions]
        if missing:
            raise ValueError(f"default_versions without a [versions.<name>] table: {missing}")
        return self


def target_dir(target_repo: Path) -> Path:
    return target_repo / TARGET_DIR_NAME


def load_target_def(target_repo: Path) -> TargetDef:
    """Read `<target_repo>/.rematch/target.toml` from the repo's working tree."""
    path = target_dir(target_repo) / TARGET_TOML
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TargetDefError(f"{path} not found") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TargetDefError(f"{path}: {exc}") from exc
    try:
        return TargetDef.model_validate(raw)
    except ValidationError as exc:
        raise TargetDefError(f"{path}: {exc}") from exc


class TomlTarget:
    """TargetAdapter backed by target.toml. Builds run through the toolchain runner."""

    def __init__(
        self,
        definition: TargetDef,
        runner: ToolchainRunner,
        build_timeout: float,
        version_override: list[str] | None = None,
    ) -> None:
        unknown = [v for v in version_override or [] if v not in definition.versions]
        if unknown:
            raise TargetDefError(f"TARGET_VERSIONS names versions not in target.toml: {unknown}")
        self._def = definition
        self._runner = runner
        self._build_timeout = build_timeout
        self._versions = list(version_override or definition.default_versions)

    @property
    def name(self) -> str:
        return self._def.name

    @property
    def definition(self) -> TargetDef:
        return self._def

    def versions(self) -> list[str]:
        return list(self._versions)

    def expected_sha1(self, version: str) -> str:
        return self._def.versions[version].sha1

    def rom_path(self, worktree: Path, version: str) -> Path:
        return worktree / self._def.versions[version].rom

    def build(self, worktree: Path, version: str) -> Path:
        argv = self._def.versions[version].build
        result = self._runner.run(argv, cwd=worktree, timeout=self._build_timeout)
        if result.returncode != 0:
            tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-20:])
            raise BuildError(f"{' '.join(argv)} exited {result.returncode}:\n{tail}")
        return self.rom_path(worktree, version)
