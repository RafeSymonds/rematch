"""Resolved configuration for target commands: settings, target and vanilla tree."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rematch.config import Settings
from rematch.doctor import vanilla_worktree_path
from rematch.target.definition import TargetDef, TargetDefError, TomlTarget, load_target_def
from rematch.toolchain.runner import ToolchainRunner


class SessionError(Exception):
    pass


@dataclass(frozen=True)
class Session:
    settings: Settings
    definition: TargetDef
    target: TomlTarget
    runner: ToolchainRunner
    worktree: Path  # vanilla worktree (answer key)

    @property
    def primary_version(self) -> str:
        return self.target.versions()[0]

    @property
    def db_path(self) -> Path:
        return self.settings.db_path(self.definition.name)

    @property
    def header_index_path(self) -> Path:
        return self.settings.rematch_var / f"{self.definition.name}.header_index.json"


def open_session(settings: Settings) -> Session:
    try:
        definition = load_target_def(settings.target_repo)
        runner = ToolchainRunner.from_spec(settings.toolchain_exec)
        target = TomlTarget(
            definition, runner, settings.build_timeout_sec, settings.version_override()
        )
    except TargetDefError as exc:
        raise SessionError(f"target.toml: {exc}") from exc
    worktree = vanilla_worktree_path(settings.target_repo)
    if not worktree.is_dir():
        raise SessionError(f"vanilla worktree {worktree} missing; run `rematch doctor`")
    return Session(settings, definition, target, runner, worktree)
