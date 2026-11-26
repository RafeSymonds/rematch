"""Linker map parsers (target.toml `[build] map_format`)."""

from __future__ import annotations

import re
from dataclasses import dataclass

# "  02000CA4 000001C8 .text   GameMain\t(src_game.c.o)"
_XMAP_LINE = re.compile(
    r"^\s+(?P<addr>[0-9A-Fa-f]{8}) (?P<size>[0-9A-Fa-f]{8}) (?P<section>\.\S+)\s+"
    r"(?P<name>\S+)\t\((?P<object>[^)]+)\)\s*$"
)


@dataclass(frozen=True)
class MapSymbol:
    name: str
    address: int
    size: int
    section: str
    object: str  # object file name as the linker reports it (library members: "lib.a member")


def parse_mwld_xmap(text: str) -> list[MapSymbol]:
    """Metrowerks linker xMAP symbol lines. Mapping symbols ($t, $a, $d, $b) are skipped."""
    out: list[MapSymbol] = []
    for line in text.splitlines():
        m = _XMAP_LINE.match(line)
        if m is None or m.group("name").startswith("$"):
            continue
        out.append(
            MapSymbol(
                name=m.group("name"),
                address=int(m.group("addr"), 16),
                size=int(m.group("size"), 16),
                section=m.group("section"),
                object=m.group("object"),
            )
        )
    return out


PARSERS = {"mwld-xmap": parse_mwld_xmap}
