"""Build a sandbox for one function (SPEC §7.1). Phase 1 writes what `./check` needs.

Phase 2 adds TASK.md, examples/, LEARNINGS.md, read-only inputs and hashing.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from rematch.context.header_index import HeaderIndex
from rematch.diff.match import function_view
from rematch.diff.objdump_backend import Line, disassemble, run_objdump
from rematch.inventory import asm_parser
from rematch.inventory.c_parser import function_source, tu_local_context
from rematch.session import Session
from rematch.target.definition import target_dir
from rematch.toolchain.compile_db import CompileDB
from rematch.toolchain.objects import read_object
from rematch.worker.check_entry import save_target
from rematch.worker.task import CompileSpec, Task, VersionTask


class SandboxError(Exception):
    pass


@dataclass(frozen=True)
class FunctionRecord:
    id: int
    name: str
    address: int
    module: str
    tu_path: str
    source_kind: str
    mode: str
    version_cond: bool


def find_function(conn: sqlite3.Connection, spec: str) -> FunctionRecord:
    """`spec` is a name, `0xADDR`, or `module:0xADDR`."""
    cols = "id, name, address, module, tu_path, source_kind, mode, version_cond"
    if ":" in spec:
        module, addr = spec.split(":", 1)
        rows = conn.execute(
            f"SELECT {cols} FROM functions WHERE module = ? AND address = ?",
            (module, int(addr, 16)),
        ).fetchall()
    elif spec.lower().startswith("0x"):
        rows = conn.execute(
            f"SELECT {cols} FROM functions WHERE address = ?", (int(spec, 16),)
        ).fetchall()
    else:
        rows = conn.execute(f"SELECT {cols} FROM functions WHERE name = ?", (spec,)).fetchall()
    if not rows:
        raise SandboxError(f"no function {spec!r} in the inventory (run `rematch inventory`)")
    if len(rows) > 1:
        options = ", ".join(f"{r[3]}:{r[2]:#010x}" for r in rows)
        raise SandboxError(f"{spec!r} is ambiguous; use one of {options}")
    r = rows[0]
    return FunctionRecord(r[0], r[1], r[2], r[3], r[4], r[5], r[6], bool(r[7]))


def _include_form(header: str, include_dirs: list[str]) -> str:
    for d in sorted(include_dirs, key=len, reverse=True):
        prefix = d.rstrip("/") + "/"
        if header.startswith(prefix):
            return header[len(prefix) :]
    return header


def context_stub(
    s: Session, fn: FunctionRecord, tu_text: str, callees: list[str], index: HeaderIndex | None
) -> str:
    """`#include` lines whose flattened content becomes ctx.h (SPEC §6.3 steps 2-3)."""
    if fn.source_kind == "c_human":
        return "".join(
            ln + "\n" for ln in tu_text.splitlines() if ln.lstrip().startswith("#include")
        )
    include_dirs = s.definition.source.include_dirs if s.definition.source else []
    headers = ["global.h"]
    if index is not None:
        for name in callees:
            for h in index.headers_for(name):
                form = _include_form(h, include_dirs)
                if form not in headers:
                    headers.append(form)
    return "".join(f'#include "{h}"\n' for h in headers)


def make_context(s: Session, version: str, stub: Path) -> str:
    ctx = s.definition.require(s.definition.context, "context")
    argv = [
        a.format(target_dir=target_dir(s.settings.target_repo), version=version, input=stub)
        for a in ctx.command
    ]
    result = s.runner.run(argv, cwd=s.worktree, timeout=120)
    if result.returncode != 0:
        raise SandboxError(f"context command failed: {result.stderr.strip()[:500]}")
    return result.stdout


def render_target(
    fn: FunctionRecord, lines: list[Line], tu_text: str, asm_format: str | None
) -> str:
    """target.s: the assembly source for asm functions, normalized disassembly otherwise."""
    if fn.source_kind == "asm" and asm_format is not None:
        for f in asm_parser.PARSERS[asm_format](tu_text):
            if f.name == fn.name:
                return f.text + "\n"
    body = [f"\t{line.mnemonic} {line.operands}".rstrip() for line in lines]
    n = sum(1 for line in lines if not line.is_data)
    return "\n".join([f"; {fn.name} ({fn.mode}, {n} instructions)", *body]) + "\n"


def check_script() -> str:
    rematch = Path(sys.executable).parent / "rematch"
    return f'#!/bin/sh\nexec "{rematch}" cd --sandbox "$(cd "$(dirname "$0")" && pwd)" "$@"\n'


def build_sandbox(s: Session, fn: FunctionRecord, out: Path, with_source: bool) -> Task:
    if out.exists() and any(out.iterdir()):
        raise SandboxError(f"{out} exists and is not empty")
    out.mkdir(parents=True, exist_ok=True)
    build = s.definition.require(s.definition.build_info, "build")
    versions = s.target.versions() if fn.version_cond else [s.primary_version]
    tu_text = (s.worktree / fn.tu_path).read_text(encoding="utf-8", errors="replace")
    index = (
        HeaderIndex.from_json(s.header_index_path.read_text(encoding="utf-8"))
        if s.header_index_path.is_file()
        else None
    )

    version_tasks: list[VersionTask] = []
    primary_lines: list[Line] = []
    for i, version in enumerate(versions):
        build_dir = s.definition.build_dir(s.worktree, version)
        db = CompileDB.load(build_dir / build.compile_commands, s.worktree)
        tu_entry = db.entry(fn.tu_path)
        entry = tu_entry if fn.source_kind == "c_human" else db.c_template_for(fn.tu_path)
        obj = read_object(tu_entry.object_path)
        lines = disassemble(run_objdump(s.settings.objdump, obj.path), obj).get(fn.name)
        if lines is None:
            raise SandboxError(f"{fn.name} not found in {tu_entry.object_path}")
        save_target(out, version, lines, function_view(obj, fn.name, lines, fn.name))
        if i == 0:
            primary_lines = lines

        callees = [ln.reloc for ln in lines if ln.reloc and not ln.reloc.startswith("<")]
        stub = out / f".ctx_stub.{version}.c"
        stub.write_text(context_stub(s, fn, tu_text, callees, index), encoding="utf-8")
        ctx = make_context(s, version, stub)
        if fn.source_kind == "c_human":
            ctx += "\n/* TU-local declarations */\n" + tu_local_context(tu_text, fn.name)
        ctx_name = "ctx.h" if i == 0 else f"ctx.{version}.h"
        (out / ctx_name).write_text(ctx, encoding="utf-8")
        version_tasks.append(
            VersionTask(
                version=version,
                compile=CompileSpec.of(entry),
                target_object=str(tu_entry.object_path),
                ctx=ctx_name,
            )
        )

    asm_format = s.definition.asm.format if s.definition.asm else None
    (out / "target.s").write_text(render_target(fn, primary_lines, tu_text, asm_format))
    source = function_source(tu_text, fn.name) if with_source else None
    if with_source and source is None:
        raise SandboxError(f"no C source for {fn.name} in {fn.tu_path}")
    candidate = source + "\n" if source else f"// Write {fn.name} here.\n"
    (out / "candidate.c").write_text(candidate, encoding="utf-8")
    check = out / "check"
    check.write_text(check_script(), encoding="utf-8")
    check.chmod(0o755)

    task = Task(
        symbol=fn.name,
        target_symbol=fn.name,
        tu=fn.tu_path,
        module=fn.module,
        mode=fn.mode,
        versions=version_tasks,
        max_checks=s.settings.max_checks,
        stagnation_limit=s.settings.stagnation_limit,
        toolchain_exec=s.settings.toolchain_exec,
        objdump=s.settings.objdump,
        compile_timeout=s.settings.compile_timeout_sec,
    )
    task.save(out)
    return task
