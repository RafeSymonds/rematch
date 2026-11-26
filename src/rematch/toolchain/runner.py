"""Run target toolchain commands, optionally through a `TOOLCHAIN_EXEC` prefix (SPEC §4)."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

CWD_PLACEHOLDER = "{cwd}"


@dataclass(frozen=True)
class ToolchainRunner:
    """Prepends `prefix` to every toolchain argv. An empty prefix runs on the host."""

    prefix: tuple[str, ...] = ()

    @classmethod
    def from_spec(cls, spec: str) -> ToolchainRunner:
        return cls(tuple(shlex.split(spec)))

    def argv(self, argv: Sequence[str], cwd: Path) -> list[str]:
        prefix = [part.replace(CWD_PLACEHOLDER, str(cwd)) for part in self.prefix]
        return [*prefix, *argv]

    def run(
        self, argv: Sequence[str], cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Run without a shell, capturing output. Raises `subprocess.TimeoutExpired`."""
        return subprocess.run(
            self.argv(argv, cwd),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
