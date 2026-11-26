from __future__ import annotations

import json
from pathlib import Path

import pytest

from rematch.diff.objdump_backend import Line
from rematch.inventory import asm_parser, link_order, mapfile
from rematch.inventory.c_parser import function_source, parse_c_functions, tu_local_context
from rematch.inventory.features import difficulty, features
from rematch.toolchain.compile_db import CompileDB, CompileDBError

FIX = Path(__file__).parent / "fixtures"


def test_nitro_lsf() -> None:
    tus = link_order.parse_nitro_lsf((FIX / "link.lsf").read_text())
    assert [(t.path, t.kind, t.module, t.overlay) for t in tus] == [
        ("src/a.c", "c", "main", None),
        ("asm/b.s", "asm", "main", None),
        ("asm/ov0.s", "asm", "OV_A", 0),
        ("src/town/x.c", "c", "town", 1),
        ("asm/town_y.s", "asm", "town", 1),
    ]
    assert [t.order for t in tus] == [0, 1, 2, 3, 4]


@pytest.mark.parametrize(
    "text",
    ["Static main\n{\nObject src/a.c\n", "Object src/a.c\n", "Static m\n{\nObject x.o\n}\n"],
)
def test_nitro_lsf_errors(text: str) -> None:
    with pytest.raises(link_order.LinkOrderError):
        link_order.parse_nitro_lsf(text)


def test_mwld_xmap() -> None:
    syms = mapfile.parse_mwld_xmap((FIX / "sample.xMAP").read_text())
    assert [(s.name, s.address, s.size, s.object) for s in syms] == [
        ("GameMain", 0x02000CA4, 0x1C8, "src_game.c.o"),
        (".version", 0x02000C88, 0x1C, "libnet.a asm_net_"),
        ("DoThing", 0x02100010, 0x38, "asm_things.s.o"),
    ]


def test_pret_macros() -> None:
    text = (FIX / "sample.s").read_text()
    funcs = asm_parser.parse_pret_macros(text)
    assert [(f.name, f.mode, f.address) for f in funcs] == [
        ("Foo", "thumb", 0x02100010),
        ("Bar", "arm", 0x02100028),
    ]
    assert funcs[0].text.startswith("\tthumb_func_start Foo")
    assert ".word 0x00000A54" in funcs[0].text
    assert funcs[0].text.rstrip().endswith("thumb_func_end Foo")
    assert asm_parser.has_version_conditional(text, ["VERSION_A", "VERSION_B"])
    assert not asm_parser.has_version_conditional(text, ["JAPAN"])
    assert not asm_parser.has_version_conditional(text, [])


@pytest.mark.parametrize(
    "text",
    [
        "\tthumb_func_start A\n\tthumb_func_start B\n",
        "\tthumb_func_start A\n\tthumb_func_end B\n",
        "\tthumb_func_start A\nA: ; 0x0\n",
    ],
)
def test_pret_macros_errors(text: str) -> None:
    with pytest.raises(asm_parser.AsmParseError):
        asm_parser.parse_pret_macros(text)


def test_c_parser_finds_definitions_only() -> None:
    text = (FIX / "sample.c").read_text()
    names = [f.name for f in parse_c_functions(text)]
    assert names == ["Helper", "Target", "Local", "GetCallback"]
    src = function_source(text, "Target")
    assert src is not None
    assert src.startswith("int Target(Thing *t, int n) {")
    assert src.rstrip().endswith("return '}';\n}")


def test_tu_local_context() -> None:
    text = (FIX / "sample.c").read_text()
    ctx = tu_local_context(text, "Target")
    assert "#include" not in ctx
    assert "#define SQUARE" in ctx
    assert "typedef struct Thing" in ctx
    assert "static int sTable[] = { 1, 2, 3 };" in ctx
    assert "return SQUARE(x);" in ctx  # inline body kept
    # Other bodies become prototypes without `static`: mwcc rejects calls to a static
    # function it never sees defined.
    assert "void Local(Thing *t);" in ctx
    assert "static void Local" not in ctx
    assert "static inline int Helper" in ctx
    assert "t->a = 0" not in ctx
    assert "int Target(" not in ctx  # the target itself is removed


def test_features_and_difficulty() -> None:
    lines = [
        Line(0, "push", "{r4, lr}"),
        Line(2, "cmp", "r0, #9"),
        Line(4, "bhi.n", ".L0"),
        Line(6, "add", "pc, r0"),
        Line(8, "bl", "Foo", reloc="Foo"),
        Line(12, "bl", "Foo", reloc="Foo"),
        Line(16, "bl", "Bar", reloc="Bar"),
        Line(20, "b.n", ".L1"),
        Line(22, "bx", "lr"),
        Line(24, ".word", "Baz", is_data=True, reloc="Baz"),
        Line(28, ".word", "0x00000a54", is_data=True),
    ]
    f = features(lines)
    assert (f.n_insns, f.n_branches, f.has_switch, f.pool_entries) == (9, 2, True, 2)
    assert f.callees == ("Foo", "Bar")
    assert difficulty(f, unknown_callees=1) == 9 + 16 + 25 + 4 + 10
    assert difficulty(f, unknown_callees=0, max_sim=1.0) == 9 + 16 + 25 + 4 - 40
    assert not features([Line(0, "pop", "{r4, pc}")]).has_switch


def test_compile_db(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    build = worktree / "build" / "v"
    build.mkdir(parents=True)
    entries = [
        {
            "directory": str(build),
            "command": "cc -DX=1 -o main.p/src_a.c.o -c ../../src/a.c",
            "file": "../../src/a.c",
            "output": "main.p/src_a.c.o",
        },
        {
            "directory": str(build),
            "arguments": ["as", "-o", "main.p/asm_b.s.o", "../../asm/b.s"],
            "file": "../../asm/b.s",
            "output": "main.p/asm_b.s.o",
        },
        {"directory": str(build), "command": "cc -c gen.c -o gen.o", "file": "/elsewhere/gen.c"},
    ]
    (build / "compile_commands.json").write_text(json.dumps(entries))
    db = CompileDB.load(build / "compile_commands.json", worktree)
    entry = db.entry("src/a.c")
    assert entry.object_path == build / "main.p" / "src_a.c.o"
    assert entry.with_io(Path("/s/scratch.c"), Path("/s/out.o")) == [
        "cc",
        "-DX=1",
        "-o",
        "/s/out.o",
        "-c",
        "/s/scratch.c",
    ]
    assert db.c_template_for("asm/b.s").file == "src/a.c"
    with pytest.raises(CompileDBError):
        db.entry("src/missing.c")
