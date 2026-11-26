from __future__ import annotations

from pathlib import Path

from rematch.diff.objdump_backend import Line
from rematch.toolchain.objects import ObjectFile, Section, Symbol
from rematch.worker.sandbox import asm_pool_names


def _obj(code: bytes, mapping: list[tuple[str, int]]) -> ObjectFile:
    syms = [Symbol("f", 0, len(code), "FUNC", "GLOBAL", 1)]
    syms += [Symbol(n, v, 0, "OBJECT" if n == "$d" else "FUNC", "LOCAL", 1) for n, v in mapping]
    return ObjectFile(Path("x.o"), {1: Section(1, ".text", code)}, syms)


def test_trailing_alignment_padding_is_not_part_of_the_function() -> None:
    padded = bytes.fromhex("70bd") * 3 + b"\0\0"  # 6 bytes of code, then .balign 4, 0
    assert _obj(padded, [("$t", 0), ("$d", 6)]).functions()[0].size == 6
    # without a data marker, or with non-zero bytes, the size is kept
    assert _obj(padded, [("$t", 0)]).functions()[0].size == 8
    assert _obj(bytes.fromhex("70bd") * 4, [("$t", 0), ("$d", 6)]).functions()[0].size == 8
    # a literal-pool word ending the function is not padding
    pool = bytes.fromhex("70bd70bd") + b"\0\0\0\0"
    assert _obj(pool, [("$t", 0), ("$d", 4)]).functions()[0].size == 8


def test_asm_pool_names_follow_source_order() -> None:
    asm = """\tthumb_func_start f
f: ; 0x02000000
\tldr r0, _02000010 ; =sTable
\tldr r1, _02000014 ; =0x1234
\tbx lr
_02000010: .word sTable
_02000014: .word 0x1234
_02000018: .word gThing + 0x10 ; comment
\tthumb_func_end f
"""
    lines = [
        Line(0, "ldr", "r0, =<data:8:abc>"),
        Line(8, ".word", "<data:8:abc>", is_data=True, reloc="<data:8:abc>"),
        Line(12, ".word", "0x00001234", is_data=True),
        Line(16, ".word", "gThing+0x10", is_data=True, reloc="gThing+0x10"),
    ]
    assert asm_pool_names(asm, lines) == {8: "sTable", 16: "gThing+0x10"}
    assert asm_pool_names(asm, lines[:2]) == {}  # counts disagree: no names
