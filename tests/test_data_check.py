"""tu_data check: data sections compare as linker concatenations, references by location."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from rematch.integrate.data_check import _compare_layout, data_relocs, exported_labels, layouts
from rematch.integrate.data_check import locator as make_locator
from rematch.toolchain.objects import ObjectFile, Reloc, Section, Symbol

ALLOC = 0x2


def sec(
    index: int, name: str, data: bytes, align: int, relocs: list[Reloc] | None = None
) -> Section:
    return Section(index, name, data, relocs or [], False, align, ALLOC, len(data))


def target_object() -> ObjectFile:
    """Vanilla asm object: one .rodata with a 2-byte value at 0 and a pointer at 4 to it."""
    rodata_sym = Symbol(".rodata", 0, 0, "SECTION", "LOCAL", 1)
    data = b"\x07\x00\x00\x00" + bytes(4)
    return ObjectFile(
        Path("t.o"),
        {1: sec(1, ".rodata", data, 4, [Reloc(4, "R_ARM_ABS32", rodata_sym, 0)])},
        [rodata_sym],
    )


def candidate() -> ObjectFile:
    """mwcc-like: the 2-byte object in its own section, the pointer table in another."""
    small = Symbol("sSmall", 0, 2, "OBJECT", "LOCAL", 2)
    table = Symbol("sTable", 0, 4, "OBJECT", "GLOBAL", 1)
    target = small
    return ObjectFile(
        Path("c.o"),
        {
            2: sec(2, ".rodata", b"\x07\x00", 4),
            1: sec(1, ".rodata", bytes(4), 4, [Reloc(0, "R_ARM_ABS32", target, 0)]),
        },
        [table, small],
    )


def saved(obj: ObjectFile) -> dict[str, dict[str, object]]:
    lays = layouts(obj)
    data_relocs(obj, lays, make_locator(lays))
    return {k: {kk: vv for kk, vv in asdict(v).items() if kk != "base"} for k, v in lays.items()}


def test_sections_concatenate_in_index_order_with_alignment() -> None:
    lays = layouts(candidate())
    assert lays[".rodata"].base == {1: 0, 2: 4}
    assert lays[".rodata"].size == 6


def test_reference_by_location_matches_and_wrong_object_fails() -> None:
    tgt = saved(target_object())[".rodata"]
    # the target has the value first; this candidate is laid out the other way round
    c = layouts(candidate())
    data_relocs(candidate(), c, make_locator(c))
    problems = _compare_layout(".rodata", tgt, c[".rodata"])
    assert any("size 0x6, target 0x8" in p for p in problems)
    assert any("layout" in p for p in problems)


def test_matching_layout_has_no_problems() -> None:
    tgt = saved(target_object())[".rodata"]
    same = target_object()
    lays = layouts(same)
    data_relocs(same, lays, make_locator(lays))
    assert _compare_layout(".rodata", tgt, lays[".rodata"]) == []


def test_exported_labels_read_global_and_public_lines() -> None:
    text = "\t.global _021028B4, _021028C4\n\t.public gFoo ; comment\n_021028B4:\n"
    assert exported_labels(text) == ["_021028B4", "_021028C4", "gFoo"]
