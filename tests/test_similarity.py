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
