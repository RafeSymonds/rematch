"""Compile two C snippets with the target's compiler and show how their code differs.

The curation agent's `./try a.c b.c` tool, and the check rematch runs on every rule's evidence.
Snippets include "types.h" (basic decomp typedefs) and nothing else.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from rematch.diff.objdump_backend import Line, disassemble, run_objdump
from rematch.diff.render import render_diff
from rematch.diff.score import align
from rematch.toolchain.compile import compile_candidate
from rematch.toolchain.compile_db import CompileEntry
from rematch.toolchain.objects import read_object
from rematch.toolchain.runner import ToolchainRunner

TYPES_H = Path(__file__).resolve().parents[3] / "prompts" / "curate" / "types.h"


@dataclass(frozen=True)
class Compiler:
    entry: CompileEntry
    runner: ToolchainRunner
    objdump: str
    timeout: float
    header: str  # written next to each snippet as types.h


@dataclass
class Trial:
    ok: bool
    errors: list[str]
    a: dict[str, list[Line]]
    b: dict[str, list[Line]]

    def differing(self) -> list[str]:
        """Instruction texts present in only one snippet, over functions both define."""
        out: list[str] = []
        for name in sorted(set(self.a) & set(self.b)):
            for op in align(self.a[name], self.b[name]):
                if op.kind == "equal":
                    continue
                for line in (op.target, op.candidate):
                    if line is not None:
                        out.append(line.text)
        return out

    def report(self) -> str:
        if not self.ok:
            return "COMPILE ERROR\n" + "\n".join(self.errors)
        parts: list[str] = []
        for name in sorted(set(self.a) | set(self.b)):
            la, lb = self.a.get(name), self.b.get(name)
            if la is None or lb is None:
                parts.append(f"{name}: only in {'a' if lb is None else 'b'}")
                continue
            diff = render_diff(align(la, lb))
            body = diff or ["(identical)"]
            parts.append(f"{name}: a {len(la)} lines, b {len(lb)} lines\n" + "\n".join(body))
        return "\n\n".join(parts)


def _compile(
    c: Compiler, source: str, work: Path, tag: str
) -> tuple[dict[str, list[Line]], list[str]]:
    work.mkdir(parents=True, exist_ok=True)
    (work / "types.h").write_text(c.header, encoding="utf-8")
    src = work / f"{tag}.c"
    src.write_text(source, encoding="utf-8")
    result = compile_candidate(c.entry, c.runner, src, work / f"{tag}.o", c.timeout)
    if not result.ok:
        return {}, [f"{tag}: {e}" for e in result.errors]
    obj = read_object(result.object_path)
    return disassemble(run_objdump(c.objdump, obj.path), obj), []


_FORBIDDEN = re.compile(r'^\s*#\s*(?:include(?!\s*"types\.h"\s*$)|pragma)', re.MULTILINE)


def modal_entry(entries: list[CompileEntry]) -> CompileEntry:
    """The C entry with the most common flags (ignoring -I/-D and file names): the main code."""

    def key(e: CompileEntry) -> tuple[str, ...]:
        return tuple(
            a
            for a in e.argv
            if not a.startswith(("-I", "-D")) and a not in (e.source_arg, e.output, "-o", "-c")
        )

    c_entries = [e for e in entries if e.is_c]
    counts = Counter(key(e) for e in c_entries)
    best = counts.most_common(1)[0][0]
    return next(e for e in c_entries if key(e) == best)


def run_trial(c: Compiler, a: str, b: str, work: Path) -> Trial:
    for src in (a, b):
        if _FORBIDDEN.search(src):
            return Trial(False, ['only #include "types.h" is allowed, and no #pragma'], {}, {})
    la, ea = _compile(c, a, work, "a")
    lb, eb = _compile(c, b, work, "b")
    return Trial(not (ea or eb), ea + eb, la, lb)


def evidence_holds(t: Trial, expect: str) -> tuple[bool, str]:
    """Both compile, their code differs, and a differing line matches `expect`."""
    if not t.ok:
        return False, "evidence does not compile: " + "; ".join(t.errors[:3])
    diff = t.differing()
    if not diff:
        return False, "the two snippets compile to the same code"
    try:
        pattern = re.compile(expect)
    except re.error as exc:
        return False, f"bad expect regex: {exc}"
    if not any(pattern.search(line) for line in diff):
        return False, f"no differing line matches {expect!r} (differing: {diff[:6]})"
    return True, "ok"
