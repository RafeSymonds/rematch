"""TargetAdapter protocol (SPEC §6.0). Phase 0 subset: versions, hashes, ROM paths, builds."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class TargetAdapter(Protocol):
    @property
    def name(self) -> str: ...

    def versions(self) -> list[str]: ...

    def expected_sha1(self, version: str) -> str: ...

    def rom_path(self, worktree: Path, version: str) -> Path: ...

    def build(self, worktree: Path, version: str) -> Path:
        """Build `version` in `worktree` and return the ROM path."""
        ...
