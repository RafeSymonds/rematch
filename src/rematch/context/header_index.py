"""Header index: declared identifier → header file, via Universal Ctags (SPEC §6.3)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Universal Ctags C kinds: d macro, e enumerator, f function, g enum, p prototype,
# s struct, t typedef, u union, v variable, x extern variable.
_KINDS = "+defgpstuvx"
FUNCTION_KINDS = frozenset({"function", "prototype"})


class HeaderIndexError(Exception):
    pass


@dataclass(frozen=True)
class Declaration:
    name: str
    header: str  # path relative to the indexed root
    kind: str


class HeaderIndex:
    def __init__(self, decls: list[Declaration]) -> None:
        self._by_name: dict[str, list[Declaration]] = {}
        for d in decls:
            self._by_name.setdefault(d.name, []).append(d)

    def __len__(self) -> int:
        return len(self._by_name)

    def headers_for(self, name: str) -> list[str]:
        return sorted({d.header for d in self._by_name.get(name, [])})

    def type_headers(self, name: str) -> list[str]:
        """Headers that define `name` as a type: typedefs first, then enum, struct and union
        tags."""
        rank = {"typedef": 0, "enum": 1, "struct": 2, "union": 3}
        hits = [d for d in self._by_name.get(name, []) if d.kind in rank]
        return list(dict.fromkeys(d.header for d in sorted(hits, key=lambda d: rank[d.kind])))

    def has_prototype(self, name: str) -> bool:
        return any(d.kind in FUNCTION_KINDS for d in self._by_name.get(name, []))

    def to_json(self) -> str:
        rows = [[d.name, d.header, d.kind] for ds in self._by_name.values() for d in ds]
        return json.dumps(rows)

    @classmethod
    def load(cls, path: Path) -> HeaderIndex:
        """`from_json` of the file, parsed once per file version and process."""
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        hit = _LOADED.get(key)
        if hit is None:
            hit = _LOADED[key] = cls.from_json(path.read_text(encoding="utf-8"))
        return hit

    @classmethod
    def from_json(cls, text: str) -> HeaderIndex:
        return cls([Declaration(n, h, k) for n, h, k in json.loads(text)])


_LOADED: dict[tuple[str, int, int], HeaderIndex] = {}


def build_header_index(ctags: str, root: Path, include_dirs: list[str]) -> HeaderIndex:
    """Index every `.h` under `include_dirs` (relative to `root`)."""
    dirs = [d for d in include_dirs if (root / d).is_dir()]
    if not dirs:
        return HeaderIndex([])
    result = subprocess.run(
        [
            ctags,
            "--output-format=json",
            "--languages=C",
            "--langmap=C:.h",
            f"--kinds-C={_KINDS}",
            "--fields=+K",
            "-R",
            "-f",
            "-",
            *dirs,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise HeaderIndexError(f"{ctags} failed: {result.stderr.strip()[:500]}")
    decls: list[Declaration] = []
    for line in result.stdout.splitlines():
        try:
            tag = json.loads(line)
        except json.JSONDecodeError:
            continue
        if tag.get("_type") != "tag":
            continue
        decls.append(Declaration(tag["name"], tag["path"], tag.get("kind", "")))
    return HeaderIndex(decls)
