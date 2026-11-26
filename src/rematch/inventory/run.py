"""`rematch inventory`: every linked function, keyed by (module, address) (SPEC §6.1)."""

from __future__ import annotations

import fnmatch
import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from rematch.context.header_index import HeaderIndex, build_header_index
from rematch.diff.objdump_backend import Line, disassemble, run_objdump
from rematch.inventory import asm_parser, link_order, mapfile
from rematch.inventory.features import difficulty, features
from rematch.target.definition import TargetDef
from rematch.toolchain.compile_db import CompileDB, CompileDBError
from rematch.toolchain.objects import ObjectFile, read_object

Log = Callable[[str], None]


@dataclass
class InventoryReport:
    started_at: str
    version: str
    tus: int = 0
    functions: Counter[str] = field(default_factory=Counter)  # by source_kind
    skipped: int = 0
    unlinked: list[str] = field(default_factory=list)  # in an object but not in the map
    address_mismatches: list[str] = field(default_factory=list)  # asm comment vs map
    missing_objects: list[str] = field(default_factory=list)
    per_module: Counter[str] = field(default_factory=Counter)
    # Assembly files matching [source] asm globs but absent from the link order: their
    # functions are not in the binary, so they are not inventoried.
    unlinked_asm_files: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "started_at": self.started_at,
                "version": self.version,
                "tus": self.tus,
                "functions": dict(self.functions),
                "skipped": self.skipped,
                "unlinked": self.unlinked,
                "address_mismatches": self.address_mismatches,
                "missing_objects": self.missing_objects,
                "per_module": dict(self.per_module),
                "unlinked_asm_files": self.unlinked_asm_files,
            },
            indent=2,
        )


@dataclass(frozen=True)
class FunctionRow:
    name: str
    address: int
    module: str
    tu_path: str
    source_kind: str
    mode: str
    lines: list[Line]
    version_cond: bool
    skipped: bool


def _matches(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in globs)


def _disassemble(objdump: str, path: Path) -> tuple[ObjectFile, dict[str, list[Line]]]:
    obj = read_object(path)
    return obj, disassemble(run_objdump(objdump, path), obj)


def collect(
    definition: TargetDef,
    worktree: Path,
    version: str,
    objdump: str,
    log: Log,
) -> tuple[list[FunctionRow], InventoryReport]:
    """Read link order, compile DB, map and vanilla objects into function rows."""
    build = definition.require(definition.build_info, "build")
    order = definition.require(definition.link_order, "link_order")
    source = definition.source
    asm_format = definition.asm.format if definition.asm else None
    report = InventoryReport(datetime.now(UTC).isoformat(timespec="seconds"), version)

    build_dir = definition.build_dir(worktree, version)
    tus = link_order.PARSERS[order.format]((worktree / order.file).read_text(encoding="utf-8"))
    db = CompileDB.load(build_dir / build.compile_commands, worktree)
    symbols = mapfile.PARSERS[build.map_format](
        (build_dir / build.map).read_text(encoding="utf-8", errors="replace")
    )
    by_object: dict[str, dict[str, mapfile.MapSymbol]] = {}
    for msym in symbols:
        by_object.setdefault(msym.object, {}).setdefault(msym.name, msym)
    report.tus = len(tus)
    log(f"{len(tus)} TUs in link order, {len(symbols)} map symbols")

    objects: dict[str, Path] = {}
    for tu in tus:
        try:
            objects[tu.path] = db.entry(tu.path).object_path
        except CompileDBError:
            report.missing_objects.append(tu.path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        disasm = dict(
            zip(
                objects,
                pool.map(lambda p: _disassemble(objdump, p), objects.values()),
                strict=True,
            )
        )

    if source is not None and asm_format is not None:
        linked = {tu.path for tu in tus}
        for pattern in source.asm:
            for path in sorted(worktree.glob(pattern)):
                rel = path.relative_to(worktree).as_posix()
                if rel not in linked and rel not in report.unlinked_asm_files:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    report.unlinked_asm_files[rel] = len(asm_parser.PARSERS[asm_format](text))
        report.unlinked_asm_files = {k: v for k, v in report.unlinked_asm_files.items() if v}

    rows: list[FunctionRow] = []
    exclude = source.exclude if source else []
    version_defines = source.version_defines if source else []
    for tu in tus:
        if tu.path not in disasm:
            continue
        obj, lines_by_name = disasm[tu.path]
        text = (worktree / tu.path).read_text(encoding="utf-8", errors="replace")
        version_cond = asm_parser.has_version_conditional(text, version_defines)
        asm_addresses: dict[str, int | None] = {}
        if tu.kind == "asm" and asm_format is not None:
            for fn in asm_parser.PARSERS[asm_format](text):
                asm_addresses[fn.name] = fn.address
        map_syms = by_object.get(objects[tu.path].name, {})
        skipped = _matches(tu.path, exclude)
        for sym in obj.functions():
            mapped = map_syms.get(sym.name)
            if mapped is None:
                report.unlinked.append(f"{tu.path}:{sym.name}")
                continue
            comment = asm_addresses.get(sym.name)
            if comment is not None and comment != mapped.address:
                report.address_mismatches.append(
                    f"{tu.path}:{sym.name} asm {comment:#010x} map {mapped.address:#010x}"
                )
            mode = obj.mode_at(sym.section, sym.value) if sym.section is not None else None
            kind = "asm" if tu.kind == "asm" else "c_human"
            rows.append(
                FunctionRow(
                    name=sym.name,
                    address=mapped.address,
                    module=tu.module,
                    tu_path=tu.path,
                    source_kind=kind,
                    mode=mode or "unknown",
                    lines=lines_by_name.get(sym.name, []),
                    version_cond=version_cond,
                    skipped=skipped,
                )
            )
            report.functions[kind] += 1
            report.per_module[tu.module] += 1
            report.skipped += int(skipped)
    return rows, report


def store_rows(conn: sqlite3.Connection, rows: list[FunctionRow], index: HeaderIndex) -> None:
    """Upsert by (module, address). Renames update `name`; status changes only for exclusions."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        for row in rows:
            f = features(row.lines)
            unknown = sum(1 for c in f.callees if not index.has_prototype(c))
            conn.execute(
                """
                INSERT INTO functions (name, address, module, tu_path, source_kind, mode,
                    n_insns, n_branches, has_switch, pool_entries, callees_json, version_cond,
                    difficulty, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (module, address) DO UPDATE SET
                    name = excluded.name, tu_path = excluded.tu_path,
                    source_kind = excluded.source_kind, mode = excluded.mode,
                    n_insns = excluded.n_insns, n_branches = excluded.n_branches,
                    has_switch = excluded.has_switch, pool_entries = excluded.pool_entries,
                    callees_json = excluded.callees_json, version_cond = excluded.version_cond,
                    difficulty = excluded.difficulty,
                    status = CASE WHEN excluded.status = 'skipped' THEN 'skipped'
                                  ELSE functions.status END,
                    updated_at = excluded.updated_at
                """,
                (
                    row.name,
                    row.address,
                    row.module,
                    row.tu_path,
                    row.source_kind,
                    row.mode,
                    f.n_insns,
                    f.n_branches,
                    int(f.has_switch),
                    f.pool_entries,
                    json.dumps(list(f.callees)),
                    int(row.version_cond),
                    difficulty(f, unknown),
                    "skipped" if row.skipped else "untried",
                    now,
                ),
            )


def run_inventory(
    definition: TargetDef,
    worktree: Path,
    version: str,
    conn: sqlite3.Connection,
    objdump: str,
    ctags: str,
    log_dir: Path,
    log: Log,
    index_path: Path,
) -> tuple[InventoryReport, Path]:
    rows, report = collect(definition, worktree, version, objdump, log)
    include_dirs = definition.source.include_dirs if definition.source else []
    index = build_header_index(ctags, worktree, include_dirs)
    log(f"header index: {len(index)} identifiers")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(index.to_json(), encoding="utf-8")
    store_rows(conn, rows, index)
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.started_at.replace(":", "").replace("-", "")
    path = log_dir / f"inventory-{stamp}.json"
    path.write_text(report.to_json(), encoding="utf-8")
    return report, path
