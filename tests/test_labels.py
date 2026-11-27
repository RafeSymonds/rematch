"""Label tables name relocations to labels the assembler keeps no symbol for (SPEC §6.5)."""

from __future__ import annotations

from pathlib import Path

from rematch.integrate.labels import defined_labels, labels_from_object, name_at, pool_names
from rematch.toolchain.objects import ObjectFile, Reloc, Section, Symbol

TEXT, RODATA = (
    Symbol(".text", 0, 0, "SECTION", "LOCAL", 1),
    Symbol(".rodata", 0, 0, "SECTION", "LOCAL", 2),
)


def test_defined_labels_include_indented_ones() -> None:
    src = "\tthumb_func_start F\nF: ; 0x0\n\tbx lr\n    _0200000C:\n\t.word 1\n.L1:\n"
    assert defined_labels(src) == ["F", "_0200000C"]


def test_jump_table_words_are_named_by_label_not_by_count() -> None:
    func = Symbol("F", 0, 0x20, "FUNC", "GLOBAL", 1)
    relocs = [
        Reloc(0x18, "R_ARM_ABS32", TEXT, 0x10),  # .word to a local label inside .text
        Reloc(0x1C, "R_ARM_ABS32", RODATA, 0x8),  # .word to label+4 in .rodata
    ]
    obj = ObjectFile(
        Path("vanilla.o"),
        {1: Section(1, ".text", bytes(0x20), relocs), 2: Section(2, ".rodata", bytes(0x10))},
        [TEXT, RODATA, func],
    )
    # the labelled copy: same layout, labels kept as symbols
    labelled = ObjectFile(
        Path("labels.o"),
        obj.sections,
        [
            Symbol("_02000010", 0x10, 0, "NOTYPE", "GLOBAL", 1),
            Symbol("_02100004", 0x4, 0, "NOTYPE", "GLOBAL", 2),
            Symbol("alias_02100004", 0x4, 0, "NOTYPE", "GLOBAL", 2),
        ],
    )
    table = labels_from_object(labelled, ["_02000010", "_02100004", "alias_02100004"])
    assert table == {".text": [(0x10, "_02000010")], ".rodata": [(0x4, "_02100004")]}
    assert name_at(table, ".rodata", 0x8) == "_02100004+0x4"
    assert name_at(table, ".rodata", 0x0) is None
    assert pool_names(obj, func, table) == {0x18: "_02000010", 0x1C: "_02100004+0x4"}
