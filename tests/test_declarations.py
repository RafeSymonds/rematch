from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rematch.context.declarations import declarations, read_tags

CTAGS = shutil.which("ctags")
pytestmark = pytest.mark.skipif(CTAGS is None, reason="ctags missing")

CTX = """typedef int s32;
typedef unsigned short u16;
#define FOO 3
typedef struct Point {
    s32 x;
    s32 y;
} Point;
typedef enum Mode { MODE_A, MODE_B } Mode;
struct Big {
    int a;
};
int Callee(Point *p, u16 n, Mode m);
extern Point gOrigin;
void sub_1000(Point *out);
void Unrelated(struct Big *b);
"""


def test_declarations_pick_what_the_function_needs(tmp_path: Path) -> None:
    assert CTAGS is not None
    ctx = tmp_path / "ctx.h"
    ctx.write_text(CTX)
    tags = read_tags(CTAGS, ctx)
    text = declarations(CTX, tags, functions=["sub_1000", "Callee"], data=["gOrigin"])
    assert "void sub_1000(Point *out);" in text
    assert "int Callee(Point *p, u16 n, Mode m);" in text
    assert "extern Point gOrigin;" in text
    assert "} Point;" in text and "MODE_A" in text  # composite types, one level
    assert "Unrelated" not in text and "struct Big" not in text and "typedef int s32" not in text
    assert text.index("} Point;") < text.index("void sub_1000")  # types first
    assert "// ctx.h:4" in text  # line references into ctx.h


def test_missing_names_give_empty_text(tmp_path: Path) -> None:
    assert CTAGS is not None
    ctx = tmp_path / "ctx.h"
    ctx.write_text(CTX)
    assert declarations(CTX, read_tags(CTAGS, ctx), ["Nope"], []) == ""
