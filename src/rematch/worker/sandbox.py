"""Build a sandbox for one function (SPEC §7.1). Phase 1 writes what `./check` needs.

Phase 2 adds TASK.md, examples/, LEARNINGS.md, read-only inputs and hashing.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from rematch import prompts
from rematch.context.header_index import HeaderIndex
from rematch.diff.match import function_view
from rematch.diff.objdump_backend import Line, disassemble, run_objdump
from rematch.inventory import asm_parser, link_order
from rematch.inventory.c_parser import function_source, strip_comments, tu_local_context
from rematch.session import Session
from rematch.target.definition import target_dir
from rematch.toolchain.compile_db import CompileDB, CompileEntry
from rematch.toolchain.objects import ObjectFile, read_object
from rematch.worker.check_entry import save_target
from rematch.worker.task import CompileSpec, Task, VersionTask

LEARNINGS = Path(__file__).resolve().parents[3] / "docs" / "LEARNINGS.md"


class SandboxError(Exception):
    pass


class ContextError(SandboxError):
    """The target's context command failed for this function's headers."""


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


def include_flags(entry: CompileEntry) -> list[str]:
    """The compile entry's `-I` directories, absolute (they are relative to its build dir)."""
    out: list[str] = []
    argv = list(entry.argv)
    for i, arg in enumerate(argv):
        path = None
        if arg == "-I" and i + 1 < len(argv):
            path = argv[i + 1]
        elif arg.startswith("-I") and len(arg) > 2:
            path = arg[2:]
        if path is not None:
            out.append(f"-I{(entry.directory / path).resolve()}")
    return out


def make_context(s: Session, version: str, stub: Path, entry: CompileEntry) -> str:
    """Run the target's context command. `{includes}` expands to the TU's own -I flags."""
    ctx = s.definition.require(s.definition.context, "context")
    argv: list[str] = []
    for a in ctx.command:
        if a == "{includes}":
            argv += include_flags(entry)
        else:
            argv.append(
                a.format(target_dir=target_dir(s.settings.target_repo), version=version, input=stub)
            )
    result = s.runner.run(argv, cwd=s.worktree, timeout=120)
    if result.returncode != 0:
        raise ContextError(f"context command failed: {result.stderr.strip()[:500]}")
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


def check_script(control: Path) -> str:
    rematch = Path(sys.executable).parent / "rematch"
    return (
        f'#!/bin/sh\nexec "{rematch}" cd --sandbox "$(cd "$(dirname "$0")" && pwd)" '
        f'--control "{control}" "$@"\n'
    )


@dataclass(frozen=True)
class SandboxOptions:
    with_source: bool = False  # seed candidate.c with the human C (round-trip/oracle only)
    bench: bool = False  # §7.2: anonymous symbol, scrubbed context
    examples: tuple[FunctionRecord, ...] = ()
    task_md: bool = True


def anonymous_name(s: Session, module: str, address: int) -> str:
    """The target's anonymous name for a function (target.toml [naming], SPEC §7.2)."""
    order = s.definition.require(s.definition.link_order, "link_order")
    tus = link_order.PARSERS[order.format]((s.worktree / order.file).read_text(encoding="utf-8"))
    overlay = next((tu.overlay for tu in tus if tu.module == module), None)
    naming = s.definition.naming
    if overlay is not None:
        return naming.overlay_anonymous.format(address=address, overlay=overlay, module=module)
    return naming.anonymous.format(address=address, module=module)


def _rename(text: str, old: str, new: str) -> str:
    return re.sub(rf"\b{re.escape(old)}\b", new, text)


def _vanilla_lines(s: Session, obj_path: Path, name: str) -> tuple[ObjectFile, list[Line]]:
    obj = read_object(obj_path)
    lines = disassemble(run_objdump(s.settings.objdump, obj.path), obj).get(name)
    if lines is None:
        raise SandboxError(f"{name} not found in {obj_path}")
    return obj, lines


def write_examples(s: Session, examples: tuple[FunctionRecord, ...], out: Path) -> list[str]:
    """examples/NN_<name>.c and .s for matched functions (SPEC §6.7)."""
    if not examples:
        return []
    build = s.definition.require(s.definition.build_info, "build")
    db = CompileDB.load(
        s.definition.build_dir(s.worktree, s.primary_version) / build.compile_commands, s.worktree
    )
    folder = out / "examples"
    folder.mkdir()
    names: list[str] = []
    for i, ex in enumerate(examples):
        text = (s.worktree / ex.tu_path).read_text(encoding="utf-8", errors="replace")
        source = function_source(text, ex.name)
        if source is None:
            continue
        _, lines = _vanilla_lines(s, db.entry(ex.tu_path).object_path, ex.name)
        stem = f"{i:02d}_{ex.name}"
        (folder / f"{stem}.c").write_text(source + "\n", encoding="utf-8")
        (folder / f"{stem}.s").write_text(render_target(ex, lines, text, None), encoding="utf-8")
        names.append(stem)
    return names


def build_sandbox(
    s: Session,
    fn: FunctionRecord,
    out: Path,
    options: SandboxOptions | None = None,
    control: Path | None = None,
) -> Task:
    """Write the sandbox at `out` and its control data (task.json, .target/) at `control`.

    Worker attempts keep the control directory outside the sandbox, under `REMATCH_VAR`, which
    workers are denied reading; it names the real symbol in bench mode. Other callers default
    to `out/.control`.
    """
    opts = options or SandboxOptions()
    if out.exists() and any(out.iterdir()):
        raise SandboxError(f"{out} exists and is not empty")
    control = (control or out / ".control").resolve()
    control.mkdir(parents=True, exist_ok=True)
    if opts.bench and fn.source_kind != "c_human":
        raise SandboxError("bench mode needs a human-C target")
    out.mkdir(parents=True, exist_ok=True)
    build = s.definition.require(s.definition.build_info, "build")
    versions = s.target.versions() if fn.version_cond else [s.primary_version]
    tu_text = (s.worktree / fn.tu_path).read_text(encoding="utf-8", errors="replace")
    symbol = anonymous_name(s, fn.module, fn.address) if opts.bench else fn.name
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
        obj, lines = _vanilla_lines(s, tu_entry.object_path, fn.name)
        save_target(control, version, lines, function_view(obj, fn.name, lines, fn.name))
        if i == 0:
            primary_lines = lines

        callees = [ln.reloc for ln in lines if ln.reloc and not ln.reloc.startswith("<")]
        stub = out / f".ctx_stub.{version}.c"
        stub.write_text(context_stub(s, fn, tu_text, callees, index), encoding="utf-8")
        ctx = make_context(s, version, stub, entry)
        stub.unlink()
        if fn.source_kind == "c_human":
            local = tu_local_context(tu_text, fn.name)
            if opts.bench:
                local = strip_comments(local)  # comments may describe or name the target
            ctx += "\n/* TU-local declarations */\n" + local
        if opts.bench:
            ctx = _rename(ctx, fn.name, symbol)
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
    shown = FunctionRecord(
        fn.id, symbol, fn.address, fn.module, fn.tu_path, fn.source_kind, fn.mode, fn.version_cond
    )
    (out / "target.s").write_text(render_target(shown, primary_lines, tu_text, asm_format))
    if opts.with_source:
        source = function_source(tu_text, fn.name)
        if source is None:
            raise SandboxError(f"no C source for {fn.name} in {fn.tu_path}")
        candidate = _rename(source, fn.name, symbol) + "\n"
    else:
        candidate = f"// Write {symbol} here. Its prototype, if known, is in ctx.h.\n"
    (out / "candidate.c").write_text(candidate, encoding="utf-8")
    (out / "NOTES.md").write_text("", encoding="utf-8")
    # Claude Code denies Write on a file that doesn't exist yet even when the path is allowed,
    # so the worker's report file exists from the start.
    (out / "SUMMARY.json").write_text("{}\n", encoding="utf-8")
    check = out / "check"
    check.write_text(check_script(control), encoding="utf-8")
    check.chmod(0o755)

    example_names = write_examples(s, opts.examples, out)
    (out / "LEARNINGS.md").write_text(LEARNINGS.read_text(encoding="utf-8"), encoding="utf-8")
    if opts.task_md:
        (out / "TASK.md").write_text(
            prompts.render(
                "task.md.j2",
                symbol=symbol,
                mode=fn.mode,
                n_insns=sum(1 for ln in primary_lines if not ln.is_data),
                module=fn.module,
                examples=example_names,
                draft=None,
                previous_best=None,
                previous_best_score=None,
                max_checks=s.settings.max_checks,
                version_cond=fn.version_cond,
            ),
            encoding="utf-8",
        )

    task = Task(
        symbol=symbol,
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
    task.save(control)
    return task


# --- read-only inputs and the hash manifest (SPEC §7.1) --------------------------------

WRITABLE = frozenset({"candidate.c", "NOTES.md", "SUMMARY.json"})
HISTORY_DIR = ".history"


def _sealed_files(sandbox: Path) -> list[Path]:
    out: list[Path] = []
    for path in sorted(sandbox.rglob("*")):
        rel = path.relative_to(sandbox)
        if rel.parts[0] == HISTORY_DIR or rel.as_posix() in WRITABLE:
            continue
        if path.is_file() or path.is_symlink():
            out.append(path)
    return out


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seal(sandbox: Path) -> dict[str, str]:
    """Make every input read-only and return its SHA-256 manifest (store it outside)."""
    manifest: dict[str, str] = {}
    for path in _sealed_files(sandbox):
        manifest[path.relative_to(sandbox).as_posix()] = _sha256(path)
        path.chmod(0o555 if path.name == "check" else 0o444)
    return manifest


def verify_seal(sandbox: Path, manifest: dict[str, str]) -> list[str]:
    """Inputs that changed, disappeared or appeared since `seal`."""
    problems: list[str] = []
    current = {p.relative_to(sandbox).as_posix(): p for p in _sealed_files(sandbox)}
    for rel, digest in manifest.items():
        path = current.get(rel)
        if path is None:
            problems.append(f"missing {rel}")
        elif _sha256(path) != digest:
            problems.append(f"modified {rel}")
    problems += [f"added {rel}" for rel in sorted(set(current) - set(manifest))]
    return problems
