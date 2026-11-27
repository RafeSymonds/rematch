"""Prototype overrides: the protos block, declaration stripping and header edits."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rematch.context.protos import (
    ProtosError,
    extract,
    remove_block,
    replace_declarations,
    strip_declarations,
)
from rematch.integrate.compose import compose_piece
from rematch.integrate.headers import header_edits, rewrite_includes
from rematch.worker.check_entry import protos_rejection

CANDIDATE = """/* rematch:types */
typedef struct UnkStruct_0205 { int unk_00; } UnkStruct_0205;
/* rematch:end-types */
/* rematch:protos */
BOOL f(u16 a, void (*cb)(int, u8 *));
// a callee whose header narrows its argument
void g(int x);
/* rematch:end-protos */
BOOL f(u16 a, void (*cb)(int, u8 *)) {
    g(a);
    return TRUE;
}
"""

HEADER = """#ifndef X_H
#define X_H
#define CALL(x) do { f(x); } while (0)
// f is still assembly
void f(void);
static inline int h(void) { return f2(); }
void (*table[2])(void);
void g(u8 x), other(void);
typedef void f_t(void);
#endif
"""


def test_extract_reads_prototypes_with_function_pointer_parameters() -> None:
    assert extract(CANDIDATE) == {
        "f": "BOOL f(u16 a, void (*cb)(int, u8 *));",
        "g": "void g(int x);",
    }
    assert "rematch:protos" not in remove_block(CANDIDATE)
    assert extract("int x;") == {}


@pytest.mark.parametrize(
    ("block", "error"),
    [
        ("/* rematch:protos */ int f(void);", "end-protos"),
        ("/* rematch:protos */ int x; /* rematch:end-protos */", "only function prototypes"),
        ("/* rematch:protos */ int f(void); int f(int); /* rematch:end-protos */", "twice"),
    ],
)
def test_extract_rejects_malformed_blocks(block: str, error: str) -> None:
    with pytest.raises(ProtosError, match=error):
        extract(block)


def test_strip_removes_declarations_not_definitions_macros_or_typedefs() -> None:
    text, found = strip_declarations(HEADER, ["f", "f2", "g"])
    assert found == {"f", "g"}
    assert "void f(void);" not in text and "g(u8 x)" not in text
    assert "#define CALL(x) do { f(x); } while (0)" in text  # a directive, kept
    assert "static inline int h(void) { return f2(); }" in text  # a definition, kept
    assert "typedef void f_t(void);" in text and "// f is still assembly" in text


def test_replace_keeps_the_rest_of_the_header() -> None:
    out = replace_declarations(HEADER, "f", "BOOL f(u16 a);")
    assert out is not None and "BOOL f(u16 a);" in out and "void f(void);" not in out
    assert out.count("\n") == HEADER.count("\n")
    assert replace_declarations(HEADER, "missing", "void missing(int);") is None


def test_header_edits_only_touch_headers_with_a_different_prototype(tmp_path: Path) -> None:
    (tmp_path / "include" / "sub").mkdir(parents=True)
    (tmp_path / "include" / "a.h").write_text("void f(void);\n")
    (tmp_path / "include" / "sub" / "b.h").write_text("int unrelated(void);\n")
    (tmp_path / "include" / "c.h").write_text("BOOL f(u16 a);\n")
    edits = header_edits(tmp_path, ["include"], {"f": "BOOL f(u16 a);"})
    assert edits == {"include/a.h": "BOOL f(u16 a);\n"}


def test_rewrite_includes_points_header_dirs_at_the_copy(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    build = worktree / "build" / "v1"
    build.mkdir(parents=True)
    (worktree / "include").mkdir()
    headers = tmp_path / "headers"
    argv = ["cc", "-I../../include", "-I", "../../include", "-I../../src", "-Ifiles"]
    out = rewrite_includes(argv, build, worktree, headers, ["include"])
    assert out == ["cc", f"-I{headers / 'include'}", "-I", str(headers / "include"),
                   "-I../../src", "-Ifiles"]  # fmt: skip


def test_compose_drops_the_block_from_pieces() -> None:
    piece = compose_piece(["global.h"], [("f", CANDIDATE)])
    assert "rematch:protos" not in piece and "void g(int x);" not in piece
    assert "BOOL f(u16 a, void (*cb)(int, u8 *));" in piece  # from the definition


def test_only_the_target_and_listed_callees_may_be_redeclared() -> None:
    task: Any = SimpleNamespace(protos_allowed=["f", "g"])
    assert protos_rejection({"f": "", "g": ""}, task) is None
    reason = protos_rejection({"memset": ""}, task)
    assert reason is not None and "not memset" in reason and "(f, g)" in reason


def test_context_headers_get_the_headers_defining_their_types_first(tmp_path: Path) -> None:
    from rematch.context.header_index import Declaration, HeaderIndex
    from rematch.worker.sandbox import with_type_dependencies

    inc = tmp_path / "include"
    (inc / "constants").mkdir(parents=True)
    (inc / "widget.h").write_text(
        "typedef struct Widget Widget;\nWidget *Widget_New(enum PoolID id, World *f);\n"
    )
    (inc / "constants" / "pool.h").write_text("enum PoolID { POOL_0 };\n")
    (inc / "types.h").write_text("typedef struct World World;\n")
    index = HeaderIndex(
        [
            Declaration("Widget", "include/widget.h", "typedef"),
            Declaration("PoolID", "include/constants/pool.h", "enum"),
            Declaration("World", "include/world.h", "struct"),
            Declaration("World", "include/types.h", "typedef"),
        ]
    )
    out = with_type_dependencies(["include/widget.h"], index, tmp_path)
    assert out == ["include/constants/pool.h", "include/types.h", "include/widget.h"]
