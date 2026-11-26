from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rematch.toolchain.runner import ToolchainRunner


def test_empty_prefix_runs_on_host(tmp_path: Path) -> None:
    runner = ToolchainRunner.from_spec("")
    assert runner.argv(["make", "x"], tmp_path) == ["make", "x"]
    result = runner.run(["pwd"], cwd=tmp_path, timeout=10)
    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_prefix_and_cwd_substitution(tmp_path: Path) -> None:
    runner = ToolchainRunner.from_spec("docker exec -i -w {cwd} 'my box'")
    assert runner.argv(["make"], tmp_path) == [
        "docker",
        "exec",
        "-i",
        "-w",
        str(tmp_path),
        "my box",
        "make",
    ]


def test_prefix_executes_without_shell(tmp_path: Path) -> None:
    runner = ToolchainRunner.from_spec("env REMATCH_CWD={cwd}")
    result = runner.run(
        ["sh", "-c", 'echo "$REMATCH_CWD"; echo $((1+1))'], cwd=tmp_path, timeout=10
    )
    assert result.stdout.split() == [str(tmp_path), "2"]


def test_timeout_raises(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        ToolchainRunner().run(["sleep", "5"], cwd=tmp_path, timeout=0.2)
