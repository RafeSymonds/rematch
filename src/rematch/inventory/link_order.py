"""Link-order parsers (target.toml `[link_order] format`)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_BLOCK = re.compile(r"^(?P<kind>Static|Autoload|Overlay)\s+(?P<name>\S+)\s*$")
_OBJECT = re.compile(r"^Object\s+(?P<path>\S+)")


class LinkOrderError(Exception):
    pass


@dataclass(frozen=True)
class LinkedTU:
    path: str  # repo-relative source path, e.g. 'asm/foo.s'
    kind: Literal["asm", "c"]
    module: str  # block name, e.g. 'main' or an overlay name
    overlay: int | None  # index among Overlay blocks (None for Static/Autoload)
    order: int  # position in link order


def parse_nitro_lsf(text: str) -> list[LinkedTU]:
    """NitroSDK LSF: `Static|Autoload|Overlay NAME { ... Object path ... }` blocks."""
    tus: list[LinkedTU] = []
    module: str | None = None
    overlay: int | None = None
    overlay_count = 0
    depth = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        block = _BLOCK.match(line)
        if block and depth == 0:
            module = block.group("name")
            if block.group("kind") == "Overlay":
                overlay = overlay_count
                overlay_count += 1
            else:
                overlay = None
            continue
        if line == "{":
            depth += 1
            continue
        if line == "}":
            depth -= 1
            if depth < 0:
                raise LinkOrderError(f"line {lineno}: unbalanced '}}'")
            continue
        obj = _OBJECT.match(line)
        if obj:
            if module is None or depth != 1:
                raise LinkOrderError(f"line {lineno}: Object outside a block")
            path = obj.group("path")
            if path.endswith(".s"):
                kind: Literal["asm", "c"] = "asm"
            elif path.endswith(".c"):
                kind = "c"
            else:
                raise LinkOrderError(f"line {lineno}: unknown TU type {path!r}")
            tus.append(LinkedTU(path, kind, module, overlay, len(tus)))
    if depth != 0:
        raise LinkOrderError("unbalanced braces at end of file")
    return tus


PARSERS = {"nitro-lsf": parse_nitro_lsf}
