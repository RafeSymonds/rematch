"""Isolated compile of a candidate with the vanilla build's exact argv (SPEC §6.4 step 3)."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from rematch.toolchain.compile_db import CompileEntry
from rematch.toolchain.runner import ToolchainRunner

MAX_ERROR_LINES = 15
SCRATCH_HEADER_LINES = 2  # the `#include` and `#line` lines scratch_source() adds
# "<path>\\scratch_v.c:46: message" (mwcc ignores #line, so map scratch lines back ourselves)
_DIAG = re.compile(
    r"^(?P<path>.*?)(?P<file>scratch[\w.]*\.c|ctx[\w.]*\.h):(?P<line>\d+):\s*(?P<msg>.*)$"
)


@dataclass(frozen=True)
class CompileResult:
    ok: bool
    object_path: Path
    errors: tuple[str, ...]
    seconds: float


def scratch_source(
    candidate: str, ctx_name: str = "ctx.h", prologue: str = "", epilogue: str = ""
) -> str:
    """`#include` the context, then reset line numbers so diagnostics point into candidate.c.
    `prologue`/`epilogue` wrap the candidate, e.g. to compile it in another ISA mode."""
    head = f'#include "{ctx_name}"\n' + (f"{prologue.rstrip()}\n" if prologue else "")
    sep = "" if candidate.endswith("\n") else "\n"
    tail = f"{sep}{epilogue.rstrip()}\n" if epilogue else ""
    return f'{head}#line 1 "candidate.c"\n{candidate}{tail}'


def tidy_errors(output: str) -> tuple[str, ...]:
    """Diagnostics with paths stripped, scratch lines mapped to candidate.c, capped at 15."""
    keep: list[str] = []
    for raw in output.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("Errors caused tool to abort"):
            continue
        if "corresponding #line reference" in line:
            continue  # mwcc's note restating the line it already reported
        m = _DIAG.match(line)
        if m:
            name, number = m.group("file"), int(m.group("line"))
            if name.startswith("scratch"):
                name, number = "candidate.c", number - SCRATCH_HEADER_LINES
            keep.append(f"{name}:{number}: {m.group('msg')}")
        elif keep:
            keep.append(line.strip())
        if len(keep) >= MAX_ERROR_LINES:
            break
    if not keep:
        keep = [ln.strip() for ln in output.splitlines() if ln.strip()][:MAX_ERROR_LINES]
    return tuple(keep)


def compile_candidate(
    entry: CompileEntry,
    runner: ToolchainRunner,
    source: Path,
    output: Path,
    timeout: float,
) -> CompileResult:
    """Compile `source` to `output` with `entry`'s argv, cwd = its build directory."""
    output.unlink(missing_ok=True)
    argv = entry.with_io(source, output)
    start = time.perf_counter()
    try:
        result = runner.run(argv, cwd=entry.directory, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CompileResult(
            False, output, (f"compile timed out after {timeout:g} s",), time.perf_counter() - start
        )
    seconds = time.perf_counter() - start
    ok = result.returncode == 0 and output.is_file()
    errors = () if ok else tidy_errors(result.stdout + "\n" + result.stderr)
    return CompileResult(ok, output, errors, seconds)
