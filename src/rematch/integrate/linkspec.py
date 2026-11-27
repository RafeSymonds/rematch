"""Edit the link-order source file (target.toml `[link_order] file`), never generated files."""

from __future__ import annotations

import re


class LinkSpecError(Exception):
    pass


def replace_objects_nitro_lsf(text: str, old: list[str], new: list[str]) -> str:
    """Replace the consecutive `Object` lines for `old` paths with lines for `new` paths."""
    lines = text.splitlines(keepends=True)
    pattern = re.compile(r"^(?P<indent>\s*)Object\s+(?P<path>\S+)\s*$")
    spots = []
    for i, line in enumerate(lines):
        m = pattern.match(line.rstrip("\n"))
        if m and m.group("path") in old:
            spots.append((i, m.group("indent"), m.group("path")))
    found = [p for _, _, p in spots]
    if found != old:
        raise LinkSpecError(f"expected Object lines {old}, found {found}")
    first, last = spots[0][0], spots[-1][0]
    if last - first != len(old) - 1:
        raise LinkSpecError(f"Object lines for {old} are not consecutive")
    indent = spots[0][1]
    newline = "\n" if lines[first].endswith("\n") else ""
    replacement = [f"{indent}Object {p}\n" for p in new]
    if replacement and not newline:
        replacement[-1] = replacement[-1].rstrip("\n")
    return "".join(lines[:first] + replacement + lines[last + 1 :])


WRITERS = {"nitro-lsf": replace_objects_nitro_lsf}
