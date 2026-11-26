"""`rematch cd` end to end, with arm-none-eabi-gcc standing in for the target compiler."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from rematch.diff.match import function_view
from rematch.diff.objdump_backend import disassemble, run_objdump
from rematch.toolchain.objects import read_object
from rematch.toolchain.runner import ToolchainRunner
from rematch.worker.check_entry import EXIT_MATCH, EXIT_NO_MATCH, EXIT_STOP, run_check, save_target
from rematch.worker.task import CompileSpec, Task, VersionTask

GCC = shutil.which("arm-none-eabi-gcc")
OBJDUMP = shutil.which("arm-none-eabi-objdump")
pytestmark = pytest.mark.skipif(not (GCC and OBJDUMP), reason="arm-none-eabi toolchain missing")

CTX = "extern int Callee(int);\nextern int gTable[];\n"
ORIGINAL = """static int Helper(int x);
int Target(int n) {
    int s = 0;
    for (int i = 0; i < n; i++) {
        s += Callee(i) + gTable[i];
    }
    return s > 100 ? 100 : s;
}
"""


def make_sandbox(tmp_path: Path, max_checks: int = 40, stagnation: int = 8) -> Path:
    assert GCC is not None and OBJDUMP is not None
    build = tmp_path / "build"
    build.mkdir()
    (build / "orig.c").write_text(CTX + ORIGINAL)
    argv = [GCC, "-mthumb", "-O2", "-ffunction-sections", "-c", "orig.c", "-o", "orig.o"]
    spec = CompileSpec(
        file="orig.c", directory=str(build), argv=argv, source_arg="orig.c", output="orig.o"
    )
    result = ToolchainRunner().run(argv, cwd=build, timeout=60)
    assert result.returncode == 0, result.stderr
    obj = read_object(build / "orig.o")
    tlines = disassemble(run_objdump(OBJDUMP, obj.path), obj)["Target"]

    sandbox = tmp_path / "sbx"
    sandbox.mkdir()
    save_target(sandbox, "v1", tlines, function_view(obj, "Target", tlines, "Target"))
    (sandbox / "ctx.h").write_text(CTX)
    Task(
        symbol="Target",
        target_symbol="Target",
        tu="orig.c",
        module="main",
        mode="thumb",
        versions=[
            VersionTask(version="v1", compile=spec, target_object=str(obj.path), ctx="ctx.h")
        ],
        max_checks=max_checks,
        stagnation_limit=stagnation,
        toolchain_exec="",
        objdump=OBJDUMP,
        compile_timeout=60,
    ).save(sandbox)
    return sandbox


def check(sandbox: Path, candidate: str) -> tuple[str, int]:
    (sandbox / "candidate.c").write_text(candidate)
    text, code, _ = run_check(sandbox)
    return text, code


def test_original_matches(tmp_path: Path) -> None:
    sandbox = make_sandbox(tmp_path)
    text, code = check(sandbox, ORIGINAL.split("\n", 1)[1])
    assert code == EXIT_MATCH and text.startswith("MATCH (check 1/40)")
    history = json.loads((sandbox / ".history" / "001.json").read_text())
    assert history["status"] == "match" and history["seconds"] > 0
    assert (sandbox / ".history" / "001.c").is_file()


def test_static_target_is_emitted(tmp_path: Path) -> None:
    sandbox = make_sandbox(tmp_path)
    text, code = check(sandbox, "static " + ORIGINAL.split("\n", 1)[1])
    assert code == EXIT_MATCH, text


def test_nonmatch_compile_fail_and_reject(tmp_path: Path) -> None:
    sandbox = make_sandbox(tmp_path)
    wrong = ORIGINAL.split("\n", 1)[1].replace("s > 100 ? 100 : s", "s > 99 ? 99 : s")
    text, code = check(sandbox, wrong)
    assert code == EXIT_NO_MATCH
    assert text.startswith("check 1/40 | score") and "~operands" in text

    text, code = check(sandbox, "int Target(int n) { return n +; }")
    assert code == EXIT_NO_MATCH and "COMPILE ERROR" in text and "candidate.c:" in text

    text, code = check(sandbox, "#pragma once\nint Target(int n) { return n; }")
    assert code == EXIT_NO_MATCH and "REJECTED" in text
    state = json.loads((sandbox / ".history" / "state.json").read_text())
    assert state["checks"] == 3


def test_check_limit_stops(tmp_path: Path) -> None:
    sandbox = make_sandbox(tmp_path, max_checks=2)
    check(sandbox, "int Target(int n) { return n; }")
    text, code = check(sandbox, "int Target(int n) { return n + 1; }")
    assert code == EXIT_STOP and text.endswith(
        "STOP: check limit (2) reached. Write SUMMARY.json and end."
    )
    text, code = check(sandbox, ORIGINAL.split("\n", 1)[1])
    assert code == EXIT_STOP and text.startswith("STOP:")  # no more checks, even a match


def test_stagnation_stops(tmp_path: Path) -> None:
    sandbox = make_sandbox(tmp_path, stagnation=2)
    check(sandbox, "int Target(int n) { return n; }")  # first score is a new best
    _, code = check(sandbox, "int Target(int n) { return n; }")
    assert code == EXIT_NO_MATCH
    text, code = check(sandbox, "int Target(int n) { return n; }")
    assert code == EXIT_STOP and "no new best score in 2 checks" in text
