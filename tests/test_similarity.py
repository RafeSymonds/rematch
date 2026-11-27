from __future__ import annotations

from pathlib import Path

import pytest

from rematch.context.similarity import Features, features, load_cache, rank, save_cache, similarity
from rematch.diff.objdump_backend import Line


def _lines(*mnems: str, calls: tuple[str, ...] = (), words: tuple[str, ...] = ()) -> list[Line]:
    out = [Line(i * 2, m, "r0, r1") for i, m in enumerate(mnems)]
    out += [Line(100 + i, "bl", c, reloc=c) for i, c in enumerate(calls)]
    out += [Line(200 + i, ".word", w, is_data=True) for i, w in enumerate(words)]
    return out


def test_similarity_weights() -> None:
    a = features(_lines("push", "adds", "ldr", "str", "pop", calls=("F", "G"), words=("0x10",)))
    assert similarity(a, a) == pytest.approx(1.0)
    b = features(_lines("push", "adds", "ldr", "str", "pop", calls=("F",), words=("0x20",)))
    # grams 4/5 shared (the bl calls are opcodes too), callees 1/2, constants 0/2
    assert similarity(a, b) == pytest.approx(0.6 * 0.8 + 0.3 * 0.5 + 0.1 * 0.0)
    empty = Features(frozenset(), frozenset(), frozenset())
    assert similarity(empty, empty) == 0.0


def test_rank_excludes_and_orders(tmp_path: Path) -> None:
    t = features(_lines("push", "adds", "ldr", "pop", calls=("F",)))
    pool = {
        1: t,
        2: features(_lines("push", "adds", "ldr", "pop", calls=("G",))),
        3: features(_lines("movs", "bx", "lr", "nop")),
        4: features(_lines("push", "adds", "ldr", "pop", calls=("F",))),
    }
    assert rank(t, pool, exclude={1}, k=2) == [4, 2]
    save_cache(tmp_path / "c.json", "pin:1", pool)
    assert load_cache(tmp_path / "c.json", "pin:1") == pool
    assert load_cache(tmp_path / "c.json", "pin:2") is None


def test_m2c_apply_prototype_and_asm() -> None:
    from rematch.context.m2c import apply_prototype, m2c_asm

    draft = "void sub_1(void *arg0, s32 arg1) {\n    arg0->unk4 = arg1 + arg10;\n}"
    decls = "// ctx.h:9\nvoid sub_1(Thing *thing, int count);\nint Other(int arg0);"
    out = apply_prototype(draft, "sub_1", decls)
    assert out.startswith("void sub_1(Thing *thing, int count) {")
    assert "thing->unk4 = count + arg10;" in out  # arg10 is not arg1
    assert apply_prototype(draft, "sub_2", decls) == draft
    lines = [
        Line(0, "cmp", "r0, #1"),
        Line(2, "bne.n", ".L0"),
        Line(4, "bl", "<self>", reloc="<self>"),
        Line(8, "ldr", 'r0, =<str:"hi">', label=".L0"),
        Line(12, ".word", "gX", is_data=True),
    ]
    asm = m2c_asm("sub_1", lines)
    assert "sub_1:\n" in asm and "\tbne .L0" in asm and ".L0:\n\tldr r0, =anon_0" in asm
    assert "\tbl sub_1" in asm and "\t.word gX" in asm
