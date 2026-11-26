"""Assembly TU parsers (target.toml `[asm] format`)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_START = re.compile(r"^\s*(?P<mode>thumb|arm)_func_start\s+(?P<name>\w+)\s*$")
_END = re.compile(r"^\s*(?:thumb|arm)_func_end\s+(?P<name>\w+)\s*$")
_LABEL = re.compile(r"^(?P<name>\w+):\s*;\s*0x(?P<addr>[0-9A-Fa-f]+)")


class AsmParseError(Exception):
    pass


@dataclass(frozen=True)
class AsmFunction:
    name: str
    mode: Literal["thumb", "arm"]
    address: int | None  # from the `NAME: ; 0xADDR` label, if present
    start_line: int  # 1-based line of the *_func_start macro
    end_line: int  # 1-based line of the *_func_end macro (inclusive)
    text: str  # start macro through end macro, including the literal pool


def parse_pret_macros(text: str) -> list[AsmFunction]:
    """`thumb_func_start NAME` / `NAME: ; 0xADDR` / ... / `thumb_func_end NAME` blocks."""
    lines = text.splitlines()
    out: list[AsmFunction] = []
    open_name: str | None = None
    open_mode: Literal["thumb", "arm"] = "thumb"
    open_line = 0
    address: int | None = None
    for i, line in enumerate(lines, 1):
        start = _START.match(line)
        if start:
            if open_name is not None:
                raise AsmParseError(f"line {i}: {start.group('name')} starts inside {open_name}")
            open_name = start.group("name")
            open_mode = "thumb" if start.group("mode") == "thumb" else "arm"
            open_line = i
            address = None
            continue
        if open_name is None:
            continue
        label = _LABEL.match(line)
        if label and label.group("name") == open_name and address is None:
            address = int(label.group("addr"), 16)
            continue
        end = _END.match(line)
        if end:
            if end.group("name") != open_name:
                raise AsmParseError(f"line {i}: end of {end.group('name')} inside {open_name}")
            body = "\n".join(lines[open_line - 1 : i])
            out.append(AsmFunction(open_name, open_mode, address, open_line, i, body))
            open_name = None
    if open_name is not None:
        raise AsmParseError(f"{open_name} has no end macro")
    return out


def has_version_conditional(text: str, version_defines: list[str]) -> bool:
    """True if any preprocessor conditional mentions one of the version macros."""
    if not version_defines:
        return False
    names = "|".join(re.escape(d) for d in version_defines)
    pattern = re.compile(rf"^\s*#\s*(?:if|ifdef|ifndef|elif)\b.*\b(?:{names})\b", re.MULTILINE)
    return pattern.search(text) is not None


PARSERS = {"pret-macros": parse_pret_macros}
