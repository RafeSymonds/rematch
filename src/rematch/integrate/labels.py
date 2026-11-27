"""Label offsets of an assembly TU (SPEC §6.5).

The assembler keeps no symbols for labels local to a file, so a relocation to one shows up as
section+offset. To name it, a copy of the TU with every label made `.global` is assembled once
with the TU's own argv, and the label offsets are read back from that object. The result is
cached by the source text and argv.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

from rematch.session import Session
from rematch.toolchain.compile_db import CompileEntry
from rematch.toolchain.objects import ObjectFile, Symbol, read_object
from rematch.toolchain.runner import ToolchainRunner

LABEL_DEF = re.compile(r"^\s*([A-Za-z_$][\w.$]*):", re.MULTILINE)
Labels = dict[str, list[tuple[int, str]]]  # section name -> sorted (offset, label)


class LabelError(Exception):
    pass


def defined_labels(text: str) -> list[str]:
    return list(dict.fromkeys(LABEL_DEF.findall(text)))


def label_table(s: Session, entry: CompileEntry) -> Labels:
    source = (entry.directory / entry.source_arg).resolve()
    text = source.read_text(encoding="utf-8", errors="replace")
    key = hashlib.sha1((text + "\0" + json.dumps(entry.argv)).encode()).hexdigest()[:16]
    if key in _TABLES:
        return _TABLES[key]
    cache = s.settings.rematch_var / "labels" / f"{key}.json"
    if cache.is_file():
        raw = json.loads(cache.read_text(encoding="utf-8"))
        _TABLES[key] = {sec: [(int(o), str(n)) for o, n in rows] for sec, rows in raw.items()}
        return _TABLES[key]
    labels = defined_labels(text)
    # workers on the same TU build this table at the same time: each assembles in its own
    # directory, and the cache is replaced atomically
    root = s.settings.rematch_workdir.expanduser() / "labels"
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=root, prefix=f"{key}-"))
    copy = work / source.name
    copy.write_text(
        text.rstrip("\n") + "\n\n" + "".join(f"\t.global {n}\n" for n in labels), encoding="utf-8"
    )
    out = work / "labels.o"
    # The copy lives outside the repo: keep includes relative to the original's folder working.
    argv = [*entry.with_io(copy, out), f"-I{source.parent}"]
    runner = ToolchainRunner.from_spec(s.settings.toolchain_exec)
    result = runner.run(argv, cwd=entry.directory, timeout=s.settings.piece_compile_timeout_sec)
    if result.returncode != 0 or not out.is_file():
        raise LabelError(f"assembling {copy.name}: {(result.stderr or result.stdout)[-500:]}")
    table = labels_from_object(read_object(out), labels)
    shutil.rmtree(work, ignore_errors=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(f".{cache.name}.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(table), encoding="utf-8")
    os.replace(tmp, cache)
    _TABLES[key] = table
    return table


_TABLES: dict[str, Labels] = {}  # in-process copies of the cache, by key


def labels_from_object(obj: ObjectFile, labels: list[str]) -> Labels:
    """Section name -> sorted (offset, label); the first label in source order wins a tie."""
    wanted = {n: i for i, n in enumerate(labels)}
    found: dict[tuple[str, int], tuple[int, str]] = {}
    for sym in obj.symbols:
        if sym.name in wanted and sym.section is not None and sym.section in obj.sections:
            key = (obj.sections[sym.section].name, sym.value)
            rank = wanted[sym.name]
            if key not in found or rank < found[key][0]:
                found[key] = (rank, sym.name)
    table: Labels = {}
    for (sec, off), (_, name) in sorted(found.items()):
        table.setdefault(sec, []).append((off, name))
    return table


def name_at(table: Labels, section: str, offset: int) -> str | None:
    """`label` or `label+0xN` for the nearest label at or before `offset` in `section`."""
    rows = table.get(section)
    if not rows:
        return None
    i = bisect.bisect_right([o for o, _ in rows], offset) - 1
    if i < 0:
        return None
    off, name = rows[i]
    return name + (f"{offset - off:+#x}" if offset != off else "")


def pool_names(obj: ObjectFile, func: Symbol, table: Labels) -> dict[int, str]:
    """Function offset -> label name for relocations that point at a section, not a symbol."""
    if func.section is None:
        return {}
    out: dict[int, str] = {}
    for r in obj.sections[func.section].relocs:
        if not func.value <= r.offset < func.value + func.size:
            continue
        t = r.symbol
        if t.section is None or t.section not in obj.sections:
            continue
        if t.type != "SECTION" and t.name and not t.name.startswith("@"):
            continue
        name = name_at(table, obj.sections[t.section].name, t.value + r.addend)
        if name is not None:
            out[r.offset - func.value] = name
    return out
