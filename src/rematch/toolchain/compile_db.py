"""`compile_commands.json`: exact compiler argv and object paths per TU (SPEC §6.4, trap 4)."""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path


class CompileDBError(Exception):
    pass


@dataclass(frozen=True)
class CompileEntry:
    file: str  # repo-relative source path, e.g. 'src/foo.c'
    directory: Path  # absolute build directory (the compiler's cwd)
    argv: tuple[str, ...]
    source_arg: str  # the source argument exactly as it appears in argv
    output: str  # object path relative to `directory`

    @property
    def object_path(self) -> Path:
        return self.directory / self.output

    @property
    def is_c(self) -> bool:
        return self.file.endswith(".c")

    def with_io(self, source: Path, output: Path) -> list[str]:
        """The same argv with only the source and output swapped."""
        argv = list(self.argv)
        swapped_source = swapped_output = False
        for i, arg in enumerate(argv):
            if arg == self.source_arg and not swapped_source:
                argv[i] = str(source)
                swapped_source = True
            elif arg == "-o" and i + 1 < len(argv) and not swapped_output:
                argv[i + 1] = str(output)
                swapped_output = True
        if not (swapped_source and swapped_output):
            raise CompileDBError(f"cannot find source/output in argv for {self.file}")
        return argv


class CompileDB:
    def __init__(self, entries: list[CompileEntry]) -> None:
        self._by_file = {e.file: e for e in entries}
        self._entries = entries

    @classmethod
    def load(cls, path: Path, worktree: Path) -> CompileDB:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CompileDBError(f"{path}: {exc}") from exc
        entries: list[CompileEntry] = []
        root = Path(os.path.normpath(worktree))
        for item in raw:
            directory = Path(item["directory"])
            argv = item.get("arguments") or shlex.split(item["command"])
            source_arg = item["file"]
            absolute = Path(os.path.normpath(directory / source_arg))
            try:
                rel = absolute.relative_to(root).as_posix()
            except ValueError:
                continue  # generated or out-of-tree sources
            output = item.get("output")
            if output is None and "-o" in argv:
                output = argv[argv.index("-o") + 1]
            if output is None:
                continue
            entries.append(CompileEntry(rel, directory, tuple(argv), source_arg, output))
        return cls(entries)

    def entry(self, tu_path: str) -> CompileEntry:
        try:
            return self._by_file[tu_path]
        except KeyError:
            raise CompileDBError(f"no compile_commands entry for {tu_path}") from None

    def c_template_for(self, tu_path: str) -> CompileEntry:
        """C compile argv for a TU with no C entry of its own (an assembly TU).

        Uses the first C entry whose object goes to the same output directory, i.e. the same
        executable and therefore the same flags.
        """
        out_dir = Path(self.entry(tu_path).output).parent
        for e in self._entries:
            if e.is_c and Path(e.output).parent == out_dir:
                return e
        raise CompileDBError(f"no C entry shares an output directory with {tu_path}")
