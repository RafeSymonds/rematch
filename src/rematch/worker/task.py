"""`task.json`: everything `./check` needs, so a sandbox is self-describing (SPEC §7.1)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from rematch.toolchain.compile_db import CompileEntry

TASK_FILE = "task.json"
TASK_VERSION = 1


class CompileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str
    directory: str
    argv: list[str]
    source_arg: str
    output: str

    @classmethod
    def of(cls, entry: CompileEntry) -> CompileSpec:
        return cls(
            file=entry.file,
            directory=str(entry.directory),
            argv=list(entry.argv),
            source_arg=entry.source_arg,
            output=entry.output,
        )

    def entry(self) -> CompileEntry:
        return CompileEntry(
            self.file, Path(self.directory), tuple(self.argv), self.source_arg, self.output
        )


class VersionTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    compile: CompileSpec
    target_object: str  # vanilla object holding the target symbol
    ctx: str  # context header the scratch file includes for this version


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_version: int = TASK_VERSION
    symbol: str  # the function candidate.c must define
    target_symbol: str  # its name in the vanilla object (differs in bench mode, §7.2)
    tu: str
    module: str
    mode: str
    versions: list[VersionTask]  # first is primary
    max_checks: int
    stagnation_limit: int
    toolchain_exec: str
    objdump: str
    compile_timeout: float
    # Lines around the candidate for its ISA mode (target.toml [worker] mode_wrap), added by
    # the harness: workers may not write #pragma.
    prologue: str = ""
    epilogue: str = ""

    def save(self, sandbox: Path) -> None:
        (sandbox / TASK_FILE).write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, sandbox: Path) -> Task:
        return cls.model_validate_json((sandbox / TASK_FILE).read_text(encoding="utf-8"))
