"""ELF reader, objdump normalization and inventory collection on objects built by a cross gcc.

Skipped when `arm-none-eabi-gcc`/`objdump` are absent. No target-derived binaries are used.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from rematch.context.header_index import HeaderIndex
from rematch.db import store
from rematch.diff.objdump_backend import disassemble, is_branch, run_objdump
from rematch.inventory.run import collect, store_rows
from rematch.target.definition import TargetDef
from rematch.toolchain.objects import read_object

GCC = shutil.which("arm-none-eabi-gcc")
OBJDUMP = shutil.which("arm-none-eabi-objdump")
pytestmark = pytest.mark.skipif(not (GCC and OBJDUMP), reason="arm-none-eabi toolchain missing")

SOURCE = r"""
extern int Callee(int);
extern int gGlobal;
const char *Name(void) { return "hello"; }
int Recurse(int n) { return n > 0 ? Recurse(n - 1) + Callee(n) : gGlobal; }
int Branchy(int n) {
    int s = 0;
    for (int i = 0; i < n; i++) { if (i & 1) s += i; else s -= Callee(i); }
    return s;
}
"""


def build(tmp_path: Path, name: str, source: str, extra: tuple[str, ...] = ()) -> Path:
    assert GCC is not None
    src = tmp_path / f"{name}.c"
    src.write_text(source)
    out = tmp_path / f"{name}.o"
    subprocess.run(
        [GCC, "-mthumb", "-O2", "-ffunction-sections", "-c", str(src), "-o", str(out), *extra],
        check=True,
        timeout=60,
    )
    return out


def test_read_object(tmp_path: Path) -> None:
    obj = read_object(build(tmp_path, "a", SOURCE))
    names = [s.name for s in obj.functions()]
    assert names == sorted(names, key=lambda n: [s.name for s in obj.functions()].index(n))
    assert {"Name", "Recurse", "Branchy"} <= set(names)
    rec = obj.function("Recurse")
    assert rec is not None and rec.section is not None
    assert obj.mode_at(rec.section, rec.value) == "thumb"
    relocs = obj.sections[rec.section].relocs
    assert any(r.symbol.name == "Callee" for r in relocs)


def test_disassemble_normalizes(tmp_path: Path) -> None:
    assert OBJDUMP is not None
    obj = read_object(build(tmp_path, "a", SOURCE))
    lines = disassemble(run_objdump(OBJDUMP, obj.path), obj)
    rec = lines["Recurse"]
    # A relocated pool word is all zeros in the object; it must still be listed (objdump -z).
    assert any(ln.mnemonic == ".word" and ln.operands == "gGlobal" for ln in rec), [
        ln.text for ln in rec
    ]
    calls = [line.operands for line in rec if line.mnemonic.startswith("bl")]
    assert "Callee" in calls and "<self>" in calls
    assert all(not line.text.startswith(("0x", "<Recurse")) for line in rec)
    name = lines["Name"]
    assert any('<str:"hello">' in line.operands for line in name), [ln.text for ln in name]
    branchy = lines["Branchy"]
    branch_ops = [ln.operands for ln in branchy if is_branch(ln.mnemonic) and ln.reloc is None]
    assert branch_ops and all(op.startswith(".L") for op in branch_ops)
    assert all(line.offset >= 0 for line in branchy)


def test_same_code_normalizes_identically(tmp_path: Path) -> None:
    """Adding unrelated functions and strings must not change a function's normalized lines."""
    assert OBJDUMP is not None
    a = read_object(build(tmp_path, "a", SOURCE))
    noise = 'const char *Other(void) { return "zzz"; }\n'
    b = read_object(build(tmp_path, "b", noise + SOURCE))
    la = disassemble(run_objdump(OBJDUMP, a.path), a)
    lb = disassemble(run_objdump(OBJDUMP, b.path), b)
    for fn in ("Name", "Recurse", "Branchy"):
        assert [x.text for x in la[fn]] == [x.text for x in lb[fn]]


def test_collect_and_store(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "a.c").write_text(SOURCE)
    build_dir = worktree / "build" / "v"
    (build_dir / "main.p").mkdir(parents=True)
    obj = build(build_dir / "main.p", "src_a.c", SOURCE)
    (build_dir / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(build_dir),
                    "command": f"cc -c ../../src/a.c -o main.p/{obj.name}",
                    "file": "../../src/a.c",
                    "output": f"main.p/{obj.name}",
                }
            ]
        )
    )
    funcs = read_object(obj).functions()
    xmap = "".join(
        f"  {0x02000000 + 0x100 * i:08X} {f.size:08X} .text   {f.name}\t({obj.name})\n"
        for i, f in enumerate(funcs)
        if f.name != "Branchy"  # Branchy plays a dead-stripped function
    )
    (build_dir / "main.map").write_text(xmap)
    (worktree / "link.lsf").write_text("Static main\n{\n    Object src/a.c\n}\n")
    (worktree / "asm").mkdir()
    (worktree / "asm" / "unused.s").write_text(
        "\tthumb_func_start X\nX: ; 0x0\n\tbx lr\n\tthumb_func_end X\n"
    )
    definition = TargetDef.model_validate(
        {
            "name": "demo",
            "default_versions": ["v"],
            "versions": {
                "v": {"build": ["true"], "rom": "r", "sha1": "a" * 40, "build_dir": "build/v"}
            },
            "build": {"map": "main.map", "map_format": "mwld-xmap"},
            "link_order": {"file": "link.lsf", "format": "nitro-lsf"},
            "source": {"asm": ["asm/*.s"]},
            "asm": {"format": "pret-macros"},
        }
    )
    assert OBJDUMP is not None
    rows, report = collect(definition, worktree, "v", OBJDUMP, lambda _m: None)
    assert {r.name for r in rows} == {"Name", "Recurse"}
    assert report.unlinked == ["src/a.c:Branchy"]
    assert report.unlinked_asm_files == {"asm/unused.s": 1}
    assert all(r.source_kind == "c_human" and r.mode == "thumb" for r in rows)

    conn = store.connect(tmp_path / "db.sqlite")
    store_rows(conn, rows, HeaderIndex([]))
    store_rows(conn, rows, HeaderIndex([]))  # idempotent
    got = conn.execute("SELECT name, module, status, callees_json FROM functions ORDER BY name")
    rows_db = got.fetchall()
    assert [r[0] for r in rows_db] == ["Name", "Recurse"]
    assert json.loads(rows_db[1][3]) == ["<self>", "Callee"] or "Callee" in rows_db[1][3]
    conn.execute("UPDATE functions SET status = 'matched' WHERE name = 'Name'")
    store_rows(conn, rows, HeaderIndex([]))
    status = conn.execute("SELECT status FROM functions WHERE name = 'Name'").fetchone()[0]
    assert status == "matched"  # re-inventory keeps progress
    assert isinstance(conn, sqlite3.Connection)
