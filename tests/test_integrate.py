from __future__ import annotations

import pytest

from rematch.integrate.compose import compose_piece, definition_header
from rematch.integrate.linkspec import LinkSpecError, replace_objects_nitro_lsf
from rematch.integrate.pret_asm import SplitError, parse_tu, plan_pieces, render_asm_pieces

TU = """#include "constants/items.h"
\t.include "macros.inc"
\t.include "foo.inc"
\t.include "global.inc"

\t.text

\tthumb_func_start FuncA
FuncA: ; 0x02000000
\tldr r0, _02000008 ; =_02100000
\tbl FuncB
\tbx lr
\t.balign 4, 0
_02000008: .word _02100000
\tthumb_func_end FuncA

\tthumb_func_start FuncB
FuncB: ; 0x0200000C
\tbx lr
\tthumb_func_end FuncB

\tthumb_func_start FuncC
FuncC: ; 0x02000010
\tbl FuncB
\tldr r0, _0200001C ; =_02100000
\tbx lr
\t.balign 4, 0
_0200001C: .word _02100000
\tthumb_func_end FuncC

\t.rodata

_02100000:
\t.word 1, 2
"""


def test_split_middle_run_keeps_data_in_the_first_piece() -> None:
    parsed = parse_tu(TU)
    pieces = plan_pieces("asm/foo.s", parsed, {"FuncB"})
    assert [(p.kind, p.path, p.functions) for p in pieces] == [
        ("asm", "asm/foo.s", ["FuncA"]),
        ("c", "src/foo_0200000C.c", ["FuncB"]),
        ("asm", "asm/foo_02000010.s", ["FuncC"]),
    ]
    texts = render_asm_pieces(parsed, pieces, c_undefined=set())
    head, tail = texts["asm/foo.s"], texts["asm/foo_02000010.s"]
    assert "\t.rodata" in head and "_02100000:" in head and "FuncC" not in head
    assert "\t.global _02100000" in head  # used by the tail piece
    assert "\t.public FuncB" in head and "\t.public FuncB" in tail  # now in C
    assert "\t.public _02100000" in tail and "\t.rodata" not in tail
    assert tail.startswith('#include "constants/items.h"') and '.include "foo.inc"' in tail


def test_whole_tu_to_c_leaves_a_data_only_piece() -> None:
    parsed = parse_tu(TU)
    pieces = plan_pieces("asm/foo.s", parsed, {"FuncA", "FuncB", "FuncC"})
    assert [(p.kind, p.path, p.functions) for p in pieces] == [
        ("asm", "asm/foo.s", []),
        ("c", "src/foo.c", ["FuncA", "FuncB", "FuncC"]),
    ]
    text = render_asm_pieces(parsed, pieces, c_undefined={"_02100000"})["asm/foo.s"]
    assert "\t.global _02100000" in text and "thumb_func_start" not in text


def test_text_only_tu_converts_completely() -> None:
    text_only = TU.split("\t.rodata")[0].replace("_02100000", "gExternal")
    pieces = plan_pieces("asm/foo.s", parse_tu(text_only), {"FuncA", "FuncB", "FuncC"})
    assert [(p.kind, p.path) for p in pieces] == [("c", "src/foo.c")]


def test_stray_text_lines_refuse_to_split() -> None:
    with pytest.raises(SplitError):
        parse_tu(TU.replace("\tthumb_func_start FuncB", "\t.word 0\n\tthumb_func_start FuncB"))


def test_linkspec_replaces_consecutive_lines() -> None:
    lsf = "Static main\n{\n    Object asm/a.s\n    Object asm/foo.s\n    Object src/b.c\n}\n"
    out = replace_objects_nitro_lsf(lsf, ["asm/foo.s"], ["asm/foo.s", "src/foo_0200000C.c"])
    assert "    Object asm/foo.s\n    Object src/foo_0200000C.c\n    Object src/b.c" in out
    with pytest.raises(LinkSpecError):
        replace_objects_nitro_lsf(lsf, ["asm/missing.s"], ["src/x.c"])


def test_compose_hoists_types_and_unifies_sibling_prototypes() -> None:
    a = (
        "/* rematch:types */\ntypedef struct UnkStruct_02000000 { int unk_00; }"
        ' UnkStruct_02000000;\n/* rematch:end-types */\n#include "ctx.h"\n'
        "extern int gX;\nu32 FuncB(int);\n"
        "UnkStruct_02000000 *FuncA(void) {\n    return (void *)FuncB(gX);\n}\n"
    )
    b = '#include "ctx.h"\nextern int gX;\nvoid *FuncB(int x)\n{\n    return 0;\n}\n'
    out = compose_piece(["global.h", "global.h"], [("FuncA", a), ("FuncB", b)])
    assert out.count('#include "global.h"') == 1 and "ctx.h" not in out
    assert "rematch:" not in out
    assert out.index("typedef struct UnkStruct") < out.index("UnkStruct_02000000 *FuncA(void);")
    assert "void *FuncB(int x);" in out and "u32 FuncB(int);" not in out
    assert out.count("extern int gX;") == 1
    assert definition_header("FuncB", b) == "void *FuncB(int x)"
