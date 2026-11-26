"""The declarations a worker needs, cut out of its own ctx.h (Phase 3, A1).

Workers spent most of their tokens grepping a ~1 MB flattened ctx.h. This puts the target's
prototype, its callees' prototypes, the externs it references and the struct/union/enum
definitions those mention (one level deep) straight into TASK.md, with ctx.h line numbers.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_IDENT = re.compile(r"\b[A-Za-z_]\w*\b")
_FUNC_KINDS = ("prototype", "function")
_DATA_KINDS = ("externvar", "variable")
_TYPE_KINDS = ("struct", "union", "enum")
MAX_LINES = 200
MAX_TYPE_LINES = 90


@dataclass(frozen=True)
class Tag:
    name: str
    kind: str
    line: int
    end: int | None
    typeref: str | None


def read_tags(ctags: str, header: Path) -> dict[str, list[Tag]]:
    result = subprocess.run(
        [
            ctags,
            "--output-format=json",
            "--languages=C",
            "--langmap=C:.h",
            "--kinds-C=+pxdtsugev",
            "--fields=+nKe",
            "-f",
            "-",
            header.name,
        ],
        cwd=header.parent,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    tags: dict[str, list[Tag]] = {}
    for line in result.stdout.splitlines():
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            continue
        if t.get("_type") != "tag" or t.get("kind") == "member":
            continue
        tags.setdefault(t["name"], []).append(
            Tag(t["name"], t.get("kind", ""), int(t["line"]), t.get("end"), t.get("typeref"))
        )
    return tags


def _first(tags: dict[str, list[Tag]], name: str, kinds: tuple[str, ...]) -> Tag | None:
    return next((t for t in tags.get(name, []) if t.kind in kinds), None)


def _span(lines: list[str], tag: Tag) -> tuple[int, int]:
    """1-based inclusive line span of a declaration: ctags' end, or up to the next `;`."""
    if tag.end is not None:
        return tag.line, int(tag.end)
    end = tag.line
    while end < len(lines) and ";" not in lines[end - 1]:
        end += 1
    return tag.line, end


def _cut(lines: list[str], span: tuple[int, int], limit: int) -> str:
    start, end = span
    body = lines[start - 1 : end]
    if len(body) > limit:
        body = [*body[:3], f"    /* ... {len(body) - 3} more lines: ctx.h:{start}-{end} */"]
    return f"// ctx.h:{start}\n" + "\n".join(body)


def declarations(
    ctx_text: str, tags: dict[str, list[Tag]], functions: list[str], data: list[str]
) -> str:
    """Declarations of `functions` and `data`, plus the composite types they mention."""
    lines = ctx_text.splitlines()
    picked: list[tuple[str, tuple[int, int]]] = []
    seen: set[tuple[int, int]] = set()

    def add(section: str, span: tuple[int, int]) -> None:
        if span not in seen:
            seen.add(span)
            picked.append((section, span))

    texts: list[str] = []
    for name in functions:
        tag = _first(tags, name, _FUNC_KINDS)
        if tag is not None:
            span = _span(lines, tag)
            add("functions", span)
            texts.append("\n".join(lines[span[0] - 1 : span[1]]))
    for name in data:
        tag = _first(tags, name, _DATA_KINDS)
        if tag is not None:
            span = _span(lines, tag)
            add("data", span)
            texts.append("\n".join(lines[span[0] - 1 : span[1]]))

    for ident in dict.fromkeys(_IDENT.findall("\n".join(texts))):
        typedef = _first(tags, ident, ("typedef",))
        target = ident
        if typedef is not None and typedef.typeref and ":" in typedef.typeref:
            kind, _, target = typedef.typeref.partition(":")
            if kind not in _TYPE_KINDS:
                continue  # typedefs of basic types (u8, s32, ...) are noise here
        tag = _first(tags, target, _TYPE_KINDS)
        if tag is not None:
            add("types", _span(lines, tag))

    out: list[str] = []
    for section in ("types", "data", "functions"):
        for sec, span in picked:
            if sec == section:
                out.append(_cut(lines, span, MAX_TYPE_LINES if section == "types" else 20))
    text = "\n\n".join(out)
    rows = text.splitlines()
    if len(rows) > MAX_LINES:
        text = "\n".join([*rows[:MAX_LINES], "/* ... truncated; grep ctx.h for more */"])
    return text
