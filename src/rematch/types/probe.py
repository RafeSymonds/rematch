"""Measure provisional type layouts with the target compiler (SPEC §16.1).

The probe compiles `sizeof` and each field's offset and size into a data object and reads the
values back from the object, so the compiler (not the `unk_XX` names) is the authority.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

from rematch.toolchain.compile import compile_candidate
from rematch.toolchain.objects import read_object
from rematch.toolchain.runner import ToolchainRunner
from rematch.types.provisional import ProvType
from rematch.worker.task import Task

PROBE_SYMBOL = "rematch_probe"


def probe_source(block: str, types: list[ProvType], ctx_include: str) -> tuple[str, list[str]]:
    """C source for the probe, and the label of each value in order."""
    values: list[str] = []
    labels: list[str] = []
    for t in types:
        values.append(f"sizeof({t.name})")
        labels.append(f"{t.name}:size")
        for f in t.fields:
            if f.bitfield:
                continue  # offsetof/sizeof are invalid for bitfields
            values.append(f"(unsigned)&((({t.name} *)0)->{f.name})")
            labels.append(f"{t.name}.{f.name}:offset")
            values.append(f"sizeof((({t.name} *)0)->{f.name})")
            labels.append(f"{t.name}.{f.name}:size")
    body = ",\n    ".join(values)
    source = (
        f'#include "{ctx_include}"\n{block}\n'
        f"const unsigned {PROBE_SYMBOL}[] = {{\n    {body}\n}};\n"
    )
    return source, labels


def measure(sandbox: Path, task: Task, block: str, types: list[ProvType]) -> dict[str, object]:
    """{type name: {"size": n, "fields": [{"name", "offset", "size"}, ...]}} from the compiler."""
    vt = task.versions[0]
    build = sandbox / ".history" / "build"
    build.mkdir(parents=True, exist_ok=True)
    source, labels = probe_source(block, types, f"../../{vt.ctx}")
    src = build / "probe.c"
    src.write_text(source, encoding="utf-8")
    out = build / "probe.o"
    result = compile_candidate(
        vt.compile.entry(),
        ToolchainRunner.from_spec(task.toolchain_exec),
        src,
        out,
        task.compile_timeout,
    )
    if not result.ok:
        raise RuntimeError("probe failed to compile: " + "; ".join(result.errors))
    obj = read_object(out)
    sym = next((s for s in obj.symbols if s.name == PROBE_SYMBOL), None)
    if sym is None or sym.section is None:
        raise RuntimeError(f"{PROBE_SYMBOL} not found in the probe object")
    data = obj.sections[sym.section].data[sym.value : sym.value + 4 * len(labels)]
    numbers = struct.unpack(f"<{len(labels)}I", data)
    values = dict(zip(labels, numbers, strict=True))
    layout: dict[str, object] = {}
    for t in types:
        fields = [
            {
                "name": f.name,
                "offset": values[f"{t.name}.{f.name}:offset"],
                "size": values[f"{t.name}.{f.name}:size"],
                "decl": f.decl,
            }
            for f in t.fields
            if not f.bitfield
        ]
        layout[t.name] = {"kind": t.kind, "size": values[f"{t.name}:size"], "fields": fields}
    return layout


def signature(entry: dict[str, object]) -> str:
    fields = entry.get("fields", [])
    assert isinstance(fields, list)
    return ",".join(f"{f['offset']}:{f['size']}" for f in sorted(fields, key=lambda f: f["offset"]))


def dumps(entry: dict[str, object]) -> str:
    return json.dumps(entry.get("fields", []))
