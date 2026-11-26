"""Disassemble objects with objdump and normalize per-function lines (SPEC §6.5 fallback backend).

Normalization makes two compilations of the same code compare equal:
- offsets are relative to the function start
- local branch targets become `.L<k>` labels, numbered by target order
- call targets and literal pool entries show the relocation target (or the literal value),
  and anonymous targets (string literals) show their contents
- the function's own name becomes `<self>`, so a renamed bench symbol still matches
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rematch.toolchain.objects import ObjectFile, Reloc, Symbol

_HEADER = re.compile(r"^[0-9a-f]+ <(?P<name>[^>]+)>:$")
_SECTION = re.compile(r"^Disassembly of section (?P<name>\S+):$")
_INSN = re.compile(r"^\s*(?P<off>[0-9a-f]+):\s+(?P<mn>\S+)(?:\s+(?P<ops>.*?))?\s*$")
_RELOC = re.compile(r"^\s*(?P<off>[0-9a-f]+): (?P<type>R_\S+)\s+(?P<target>.*?)\s*$")
_BRANCH_TARGET = re.compile(r"^(?P<addr>[0-9a-f]+) <(?P<sym>[^>+]+)(?:\+0x(?P<add>[0-9a-f]+))?>$")
_POOL_REF = re.compile(r"@ \((?P<addr>[0-9a-f]+) <[^>]*>\)")
_DATA = (".word", ".short", ".byte", ".2byte", ".4byte")


@dataclass(frozen=True)
class Line:
    offset: int  # relative to the function start
    mnemonic: str
    operands: str  # normalized
    is_data: bool = False
    reloc: str | None = None  # normalized relocation target at this line, if any

    @property
    def text(self) -> str:
        return f"{self.mnemonic} {self.operands}".strip()


def is_branch(mnemonic: str) -> bool:
    """Conditional or unconditional branch, not a call or return (`bl`, `blx`, `bx`)."""
    base = mnemonic.split(".")[0]
    return base == "b" or (
        base.startswith("b")
        and len(base) == 3
        and base[1:]
        in {
            "eq",
            "ne",
            "cs",
            "hs",
            "cc",
            "lo",
            "mi",
            "pl",
            "vs",
            "vc",
            "hi",
            "ls",
            "ge",
            "lt",
            "gt",
            "le",
            "al",
        }
    )


def is_call(line: Line) -> bool:
    return line.mnemonic.split(".")[0] in ("bl", "blx") and line.reloc is not None


def run_objdump(objdump: str, path: Path, timeout: float = 60) -> str:
    result = subprocess.run(
        [objdump, "-d", "-r", "--no-show-raw-insn", str(path)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{objdump} failed on {path}: {result.stderr.strip()}")
    return result.stdout


# mwcc names function-scope statics `name$NNNN`, with a counter that depends on the whole TU.
_LOCAL_STATIC = re.compile(r"^(?P<stem>.+)\$\d+$")


def describe_target(obj: ObjectFile, sym: Symbol, addend: int, self_name: str | None) -> str:
    """How a relocation target is shown and compared.

    Named symbols compare by name. The function itself is `<self>`. Anonymous targets
    (string literals, section-relative) and function-scope statics compare by content.
    """
    if self_name is not None and sym.name == self_name:
        return "<self>" + (f"+{addend:#x}" if addend else "")
    local_static = _LOCAL_STATIC.match(sym.name) if sym.bind == "LOCAL" else None
    if local_static:
        return f"{local_static.group('stem')}${content_key(obj, sym, 0)}" + (
            f"{addend:+#x}" if addend else ""
        )
    if sym.name and not sym.name.startswith("@") and sym.type != "SECTION":
        return sym.name + (f"{addend:+#x}" if addend else "")
    return content_key(obj, sym, addend)


def content_key(obj: ObjectFile, sym: Symbol, addend: int) -> str:
    """Anonymous or section-relative target: identify it by the bytes it points at."""
    if sym.section is None or sym.section not in obj.sections:
        return f"<anon:{sym.name or '?'}{addend:+#x}>"
    section = obj.sections[sym.section]
    start = sym.value + addend
    limit = sym.value + sym.size if section.nobits else len(section.data)
    if sym.type == "SECTION":
        covering = obj.symbol_at(sym.section, start)
        end = covering.value + covering.size if covering and covering.size else limit
    else:
        end = sym.value + sym.size if sym.size else limit
    blob = bytes(max(end - start, 0)) if section.nobits else section.data[start:end]
    text = blob.split(b"\x00", 1)[0]
    if blob and len(text) + 1 <= len(blob) and all(32 <= b < 127 for b in text):
        return f'<str:"{text.decode()}">'
    if section.nobits:
        return f"<bss:{len(blob)}>"
    return f"<data:{len(blob)}:{hashlib.sha1(blob).hexdigest()[:10]}>"


def disassemble(
    objdump_text: str, obj: ObjectFile, self_names: dict[str, str] | None = None
) -> dict[str, list[Line]]:
    """Split objdump output into normalized lines per function symbol.

    `self_names` maps a function name to the name its self-references should be treated as
    (normally itself).
    """
    funcs = {sym.name: sym for sym in obj.functions()}
    raw: dict[str, list[tuple[int, str, str]]] = {}
    relocs: dict[str, dict[int, str]] = {}
    current: str | None = None
    for line in objdump_text.splitlines():
        if _SECTION.match(line):
            current = None
            continue
        header = _HEADER.match(line)
        if header:
            name = header.group("name")
            current = name if name in funcs else current
            if name in funcs:
                raw.setdefault(name, [])
                relocs.setdefault(name, {})
            continue
        if current is None:
            continue
        sym = funcs[current]
        reloc = _RELOC.match(line)
        if reloc:
            off = int(reloc.group("off"), 16) - sym.value
            relocs[current][off] = reloc.group("type")
            continue
        insn = _INSN.match(line)
        if insn:
            off = int(insn.group("off"), 16) - sym.value
            if 0 <= off < sym.size:
                raw[current].append((off, insn.group("mn"), insn.group("ops") or ""))

    out: dict[str, list[Line]] = {}
    for name, entries in raw.items():
        sym = funcs[name]
        self_name = (self_names or {}).get(name, name)
        section = obj.sections.get(sym.section) if sym.section is not None else None
        by_off: dict[int, Reloc] = {}
        if section is not None:
            for r in section.relocs:
                if sym.value <= r.offset < sym.value + sym.size:
                    by_off[r.offset - sym.value] = r
        out[name] = _normalize(entries, by_off, obj, sym, self_name)
    return out


def _word_value(section_data: bytes, offset: int) -> int:
    return int.from_bytes(section_data[offset : offset + 4], "little")


def _normalize(
    entries: list[tuple[int, str, str]],
    by_off: dict[int, Reloc],
    obj: ObjectFile,
    sym: Symbol,
    self_name: str,
) -> list[Line]:
    data = obj.sections[sym.section].data if sym.section is not None else b""

    def target_at(off: int) -> str | None:
        r = by_off.get(off)
        if r is None:
            return None
        addend = r.addend
        if "THM_CALL" in r.type or "THM_PC22" in r.type:
            addend += 4  # Thumb BL encodes PC-relative; the -4 is not part of the target
        elif r.type in ("R_ARM_CALL", "R_ARM_JUMP24", "R_ARM_PC24"):
            addend += 8
        return describe_target(obj, r.symbol, addend, self_name)

    # Branch labels: ordinal among distinct in-function targets.
    targets: set[int] = set()
    for off, mn, ops in entries:
        if is_branch(mn) and off not in by_off:
            m = _BRANCH_TARGET.match(ops.split("@")[0].strip())
            if m:
                targets.add(int(m.group("addr"), 16) - sym.value)
    labels = {t: f".L{i}" for i, t in enumerate(sorted(targets))}

    lines: list[Line] = []
    for off, mn, ops in entries:
        reloc = target_at(off)
        is_data = mn in _DATA
        clean = ops.split("@")[0].strip().replace("\t", " ")
        if is_data and mn == ".word":
            value = reloc if reloc is not None else f"{_word_value(data, sym.value + off):#010x}"
            lines.append(Line(off, mn, value, is_data=True, reloc=reloc))
            continue
        if is_data:
            lines.append(Line(off, mn, clean, is_data=True, reloc=reloc))
            continue
        if reloc is not None and mn.split(".")[0] in ("bl", "blx", "b", "bx"):
            lines.append(Line(off, mn, reloc, reloc=reloc))
            continue
        if mn.split(".")[0] in ("bl", "blx"):
            # A call the assembler resolved without a relocation (callee in the same section):
            # show and compare it as a call to that symbol, like a relocated call.
            m = _BRANCH_TARGET.match(clean)
            if m and m.group("add") is None:
                callee = m.group("sym")
                callee = "<self>" if callee == self_name else callee
                lines.append(Line(off, mn, callee, reloc=callee))
                continue
        if is_branch(mn):
            m = _BRANCH_TARGET.match(clean)
            if m:
                tgt = int(m.group("addr"), 16) - sym.value
                clean = labels.get(tgt, f"<out:{tgt:#x}>")
            lines.append(Line(off, mn, clean, reloc=reloc))
            continue
        pool = _POOL_REF.search(ops)
        if pool and mn.split(".")[0].startswith("ldr"):
            pool_off = int(pool.group("addr"), 16) - sym.value
            pool_value = target_at(pool_off)
            if pool_value is None:
                pool_value = f"{_word_value(data, sym.value + pool_off):#x}"
            dest = clean.split(",")[0].strip()
            lines.append(Line(off, mn, f"{dest}, ={pool_value}", reloc=reloc))
            continue
        lines.append(Line(off, mn, clean, reloc=reloc))
    return lines
