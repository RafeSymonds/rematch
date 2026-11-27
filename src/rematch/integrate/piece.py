"""Check a C piece: compile it and binary-match every function against the original.

Shared by the integrator (before writing anything) and by `./check` in a `tu_fix` sandbox,
which only has its control directory: compile specs, toolchain, and each function's target
lines and view per version (saved like a function sandbox's target cache).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rematch.diff.match import binary_match, function_view
from rematch.diff.objdump_backend import disassemble, run_objdump
from rematch.diff.render import render_diff
from rematch.diff.score import align, score
from rematch.toolchain.compile import compile_candidate
from rematch.toolchain.objects import ObjectFile, read_object
from rematch.toolchain.runner import ToolchainRunner
from rematch.worker.check_entry import load_target, save_target
from rematch.worker.task import CompileSpec

DATA_SECTIONS = (".data", ".rodata", ".bss", ".sdata", ".sbss", ".init", ".ctor", ".sinit")
PIECE_FILE = "piece.json"


@dataclass
class PieceSpec:
    """What a piece must reproduce, per version (written to the control directory)."""

    tu: str
    functions: list[str]
    compile: dict[str, dict[str, object]]  # version -> CompileSpec fields
    offsets: dict[str, dict[str, int]]  # version -> function -> offset from the first
    toolchain_exec: str
    objdump: str
    timeout: float
    max_checks: int = 40

    def save(self, control: Path) -> None:
        control.mkdir(parents=True, exist_ok=True)
        (control / PIECE_FILE).write_text(json.dumps(asdict(self)), encoding="utf-8")

    @classmethod
    def load(cls, control: Path) -> PieceSpec:
        return cls(**json.loads((control / PIECE_FILE).read_text(encoding="utf-8")))


def save_targets(control: Path, version: str, name: str, lines, view) -> None:  # type: ignore[no-untyped-def]
    save_target(control / "targets" / name, version, lines, view)


@dataclass
class FunctionStatus:
    name: str
    match: bool
    score: float
    detail: list[str] = field(default_factory=list)


@dataclass
class PieceResult:
    ok: bool
    errors: list[str] = field(default_factory=list)  # compile errors or structural problems
    functions: list[FunctionStatus] = field(default_factory=list)
    undefined: set[str] = field(default_factory=set)

    @property
    def matched(self) -> int:
        return sum(f.match for f in self.functions)

    def report(self, limit: int = 60) -> str:
        lines = list(self.errors)
        for f in self.functions:
            lines.append(f"{f.name}: {'MATCH' if f.match else f'{f.score:.1f}'}")
            if not f.match:
                lines += [f"    {d}" for d in f.detail[:limit]]
        return "\n".join(lines)


def linked_offsets(obj: ObjectFile) -> dict[str, int]:
    """Function offsets once the object's code sections are laid out in order, each aligned
    (mwcc gives every function its own `.text` section)."""
    out: dict[str, int] = {}
    cursor = 0
    placed: dict[int, int] = {}
    for sym in obj.functions():
        if sym.section is None:
            continue
        if sym.section not in placed:
            sec = obj.sections[sym.section]
            cursor = -(-cursor // sec.align) * sec.align
            placed[sym.section] = cursor
            cursor += len(sec.data)
        out[sym.name] = placed[sym.section] + sym.value
    return out


def undefined_symbols(obj: ObjectFile) -> set[str]:
    return {
        sym.name
        for sym in obj.symbols
        if sym.section is None and sym.name and sym.bind in ("GLOBAL", "WEAK")
    }


def data_sections(obj: ObjectFile) -> list[str]:
    out = []
    for sec in obj.sections.values():
        if not sec.name.startswith(DATA_SECTIONS):
            continue
        defined = any(sym.section == sec.index and sym.size for sym in obj.symbols)
        if sec.data or (sec.nobits and defined):
            out.append(sec.name)
    return out


def check_source(spec: PieceSpec, control: Path, source: str, work: Path) -> PieceResult:
    runner = ToolchainRunner.from_spec(spec.toolchain_exec)
    result = PieceResult(True)
    worst: dict[str, FunctionStatus] = {}
    for version, cs in spec.compile.items():
        src = work / f"piece.{version}.c"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(source, encoding="utf-8")
        compiled = compile_candidate(
            CompileSpec.model_validate(cs).entry(),
            runner,
            src,
            src.with_suffix(".o"),
            spec.timeout,
        )
        if not compiled.ok:
            return PieceResult(False, [f"{version}: {e}" for e in compiled.errors[:12]])
        obj = read_object(compiled.object_path)
        result.undefined |= undefined_symbols(obj)
        emitted = [f.name for f in obj.functions()]
        if emitted != spec.functions:
            result.errors.append(f"{version}: emits {emitted}, expected {spec.functions}")
        if data := data_sections(obj):
            result.errors.append(f"{version}: emits data sections {data}; use extern")
        clines = disassemble(run_objdump(spec.objdump, obj.path), obj)
        offsets = linked_offsets(obj)
        for name in spec.functions:
            tlines, tview = load_target(control / "targets" / name, version)
            if name not in clines or obj.function(name) is None:
                worst[name] = FunctionStatus(name, False, 0.0, [f"{version}: not emitted"])
                continue
            m = binary_match(tview, function_view(obj, name, clines[name], name))
            ops = align(tlines, clines[name])
            st = FunctionStatus(name, m.match, score(ops, len(tlines), len(clines[name])))
            if not m.match:
                st.detail = [f"{version}: {r}" for r in m.reasons] + render_diff(ops)
            if offsets.get(name) != spec.offsets[version][name]:
                st.match = False
                st.detail.insert(
                    0,
                    f"{version}: at +{offsets.get(name, -1):#x}, original"
                    f" +{spec.offsets[version][name]:#x}",
                )
            prev = worst.get(name)
            if prev is None or (prev.match and not st.match) or st.score < prev.score:
                worst[name] = st
    result.functions = [worst[n] for n in spec.functions if n in worst]
    result.ok = not result.errors and all(f.match for f in result.functions)
    return result
