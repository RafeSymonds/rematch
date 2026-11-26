"""Read relocatable ELF objects: sections, symbols, relocations (shared by inventory and diff)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from elftools.elf.descriptions import describe_reloc_type
from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection
from elftools.elf.sections import SymbolTableSection

# ARM mapping symbols mark code mode and data, not functions.
MAPPING_PREFIX = "$"


@dataclass(frozen=True)
class Symbol:
    name: str
    value: int
    size: int
    type: str  # FUNC, OBJECT, NOTYPE, SECTION, FILE
    bind: str  # LOCAL, GLOBAL, WEAK
    section: int | None  # section index, None for undefined/absolute/common

    @property
    def is_mapping(self) -> bool:
        return self.name.startswith(MAPPING_PREFIX)


@dataclass(frozen=True)
class Reloc:
    offset: int  # within the relocated section
    type: str  # e.g. R_ARM_THM_CALL
    symbol: Symbol
    addend: int  # explicit (RELA) or decoded from the relocated bytes (REL)
    width: int = 4  # bytes at `offset` that the relocation rewrites


def _sign(value: int, bits: int) -> int:
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def implicit_addend(rtype: str, data: bytes, offset: int) -> tuple[int, int]:
    """(addend, width) for a REL relocation, decoded from the bytes it patches."""
    word = int.from_bytes(data[offset : offset + 4], "little")
    if rtype in ("R_ARM_THM_CALL", "R_ARM_THM_PC22", "R_ARM_THM_JUMP24", "R_ARM_THM_XPC22"):
        hi = word & 0xFFFF
        lo = word >> 16
        return _sign(((hi & 0x7FF) << 12) | ((lo & 0x7FF) << 1), 23), 4
    if rtype in ("R_ARM_CALL", "R_ARM_JUMP24", "R_ARM_PC24", "R_ARM_XPC25"):
        return _sign(word & 0xFFFFFF, 24) << 2, 4
    if rtype in ("R_ARM_THM_JUMP11", "R_ARM_THM_PC11"):
        return _sign((word & 0x7FF) << 1, 12), 2
    if rtype in ("R_ARM_THM_JUMP8", "R_ARM_THM_PC9"):
        return _sign((word & 0xFF) << 1, 9), 2
    return _sign(word, 32), 4  # R_ARM_ABS32, R_ARM_REL32 and other data words


def site_width(rtype: str) -> int:
    return (
        2
        if rtype in ("R_ARM_THM_JUMP11", "R_ARM_THM_PC11", "R_ARM_THM_JUMP8", "R_ARM_THM_PC9")
        else 4
    )


@dataclass
class Section:
    index: int
    name: str
    data: bytes  # empty for NOBITS (.bss)
    relocs: list[Reloc] = field(default_factory=list)
    nobits: bool = False


@dataclass
class ObjectFile:
    path: Path
    sections: dict[int, Section]
    symbols: list[Symbol]

    def functions(self) -> list[Symbol]:
        """Sized function symbols, excluding mapping symbols, in (section, offset) order."""
        funcs = [
            s
            for s in self.symbols
            if s.type == "FUNC" and s.size > 0 and not s.is_mapping and s.section is not None
        ]
        return sorted(funcs, key=lambda s: (s.section or 0, s.value))

    def function(self, name: str) -> Symbol | None:
        for sym in self.functions():
            if sym.name == name:
                return sym
        return None

    def mode_at(self, section: int, offset: int) -> str | None:
        """ARM mode from the last `$a`/`$t` mapping symbol at or before `offset`, if any."""
        best: Symbol | None = None
        for sym in self.symbols:
            if (
                sym.section == section
                and sym.name in ("$a", "$t")
                and sym.value <= offset
                and (best is None or sym.value >= best.value)
            ):
                best = sym
        if best is None:
            return None
        return "thumb" if best.name == "$t" else "arm"

    def symbol_at(self, section: int, offset: int) -> Symbol | None:
        """A named, sized data or function symbol covering `offset` in `section`."""
        for sym in self.symbols:
            if (
                sym.section == section
                and not sym.is_mapping
                and sym.type in ("OBJECT", "FUNC")
                and sym.value <= offset < sym.value + max(sym.size, 1)
            ):
                return sym
        return None


def read_object(path: Path) -> ObjectFile:
    with path.open("rb") as handle:
        elf = ELFFile(handle)
        sections: dict[int, Section] = {}
        for index, esec in enumerate(elf.iter_sections()):
            nobits = esec["sh_type"] == "SHT_NOBITS"
            data = b"" if nobits else esec.data()
            sections[index] = Section(index=index, name=esec.name, data=data, nobits=nobits)

        symbols: list[Symbol] = []
        symtab = elf.get_section_by_name(".symtab")
        if isinstance(symtab, SymbolTableSection):
            for esym in symtab.iter_symbols():
                shndx = esym["st_shndx"]
                symbols.append(
                    Symbol(
                        name=esym.name,
                        value=esym["st_value"],
                        size=esym["st_size"],
                        type=esym["st_info"]["type"].removeprefix("STT_"),
                        bind=esym["st_info"]["bind"].removeprefix("STB_"),
                        section=shndx if isinstance(shndx, int) else None,
                    )
                )

        for rsec in elf.iter_sections():
            if not isinstance(rsec, RelocationSection):
                continue
            target = sections.get(rsec["sh_info"])
            if target is None:
                continue
            for rel in rsec.iter_relocations():
                rtype = describe_reloc_type(rel["r_info_type"], elf)
                if rel.is_RELA():
                    addend, width = rel["r_addend"], site_width(rtype)
                else:
                    addend, width = implicit_addend(rtype, target.data, rel["r_offset"])
                target.relocs.append(
                    Reloc(
                        offset=rel["r_offset"],
                        type=rtype,
                        symbol=symbols[rel["r_info_sym"]],
                        addend=addend,
                        width=width,
                    )
                )
        for section in sections.values():
            section.relocs.sort(key=lambda r: r.offset)
    return ObjectFile(path=path, sections=sections, symbols=symbols)
