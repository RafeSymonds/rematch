"""`tu_data` check: a C file that defines a TU's functions and its data (SPEC §10, Phase 4b).

The target is the vanilla assembly object of the TU, which holds the functions in `.text` and
the data in one section per type. The candidate is an mwcc object, which may split a type
over several sections; the linker concatenates them in object order, each aligned, so they
are compared as one concatenation per type.

References into data compare by location (section type and offset), never by name: a label
in the assembly may become a static, an anonymous string literal, or keep its name, and two
same-size objects that trade places must not pass. Everything the vanilla side needs is
computed once with a Session (`prepare_data`) and saved in the control directory, so the
sandbox's `./check` (`check_data`) needs only that directory.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rematch.diff.match import FunctionView, RelocEntry, Resolver, binary_match, function_view
from rematch.diff.objdump_backend import describe_target, disassemble, run_objdump
from rematch.integrate.headers import committed_headers, compile_spec
from rematch.integrate.labels import Labels, label_table, name_at
from rematch.integrate.piece import FunctionStatus, PieceResult, linked_offsets
from rematch.session import Session
from rematch.toolchain.compile import compile_candidate
from rematch.toolchain.compile_db import CompileDB
from rematch.toolchain.objects import ObjectFile, Symbol, read_object
from rematch.toolchain.runner import ToolchainRunner
from rematch.worker.task import CompileSpec

DATA_TYPES = (".data", ".rodata", ".bss", ".sdata", ".sbss")
DATA_FILE = "data.json"
_EXPORT = re.compile(r"^\s*\.(?:global|public)\s+(.+)$", re.MULTILINE)


@dataclass
class Layout:
    """One section type of an object, as the linker lays it out."""

    size: int = 0
    align: int = 1  # of the first section: where the linker starts this object's part
    data: str = ""  # hex, relocation sites zeroed; empty for NOBITS
    relocs: list[tuple[int, str, str]] = field(default_factory=list)  # offset, type, target
    symbols: list[tuple[int, str, int]] = field(default_factory=list)  # offset, name, size
    base: dict[int, int] = field(default_factory=dict)  # section index -> offset (not saved)


def layouts(obj: ObjectFile) -> dict[str, Layout]:
    out: dict[str, Layout] = {}
    raw: dict[str, bytearray] = {}
    for sec in sorted(obj.sections.values(), key=lambda s: s.index):
        if sec.name not in DATA_TYPES or not sec.alloc:
            continue
        lay = out.setdefault(sec.name, Layout(align=sec.align))
        buf = raw.setdefault(sec.name, bytearray())
        start = -(-lay.size // sec.align) * sec.align
        lay.base[sec.index] = start
        size = sec.size if sec.nobits else len(sec.data)
        if not sec.nobits:
            buf.extend(bytes(start - len(buf)))
            buf.extend(sec.data)
        lay.size = start + size
    for name, lay in out.items():
        lay.data = raw[name].hex() if raw[name] else ""
    for sym in obj.symbols:
        index = sym.section
        if index is None or sym.type == "SECTION" or not sym.name:
            continue
        for lay in out.values():
            if index in lay.base:
                lay.symbols.append((lay.base[index] + sym.value, sym.name, sym.size))
    for lay in out.values():
        lay.symbols.sort()
    return out


def locator(lays: dict[str, Layout], labels: Labels | None = None) -> Resolver:
    """Name a target by location when it lies in a data section; else by a `.text` label."""
    where = {idx: (name, base) for name, lay in lays.items() for idx, base in lay.base.items()}

    def resolve(sym: Symbol, addend: int) -> str | None:
        if sym.section in where:
            kind, base = where[sym.section]
            return f"<{kind}+{base + sym.value + addend:#x}>"
        if labels is not None and sym.section is not None and sym.type == "SECTION":
            return name_at(labels, sym.name, sym.value + addend)
        return None

    return resolve


def data_relocs(obj: ObjectFile, lays: dict[str, Layout], resolve: Resolver) -> None:
    """Fill each layout's relocations (resolved targets) and zero their sites in `data`."""
    for lay in lays.values():
        buf = bytearray.fromhex(lay.data) if lay.data else bytearray()
        for idx, base in lay.base.items():
            for r in obj.sections[idx].relocs:
                off = base + r.offset
                target = resolve(r.symbol, r.addend) or describe_target(
                    obj, r.symbol, r.addend, None
                )
                lay.relocs.append((off, r.type, target))
                buf[off : off + r.width] = bytes(r.width)
        lay.relocs.sort()
        lay.data = buf.hex() if buf else ""


def _view_json(v: FunctionView) -> dict[str, object]:
    return {
        "name": v.name,
        "size": v.size,
        "code": v.code.hex(),
        "relocs": [asdict(e) for e in v.relocs],
    }


def _view(d: dict[str, object]) -> FunctionView:
    relocs = tuple(RelocEntry(**e) for e in d["relocs"])  # type: ignore[attr-defined]
    return FunctionView(str(d["name"]), int(d["size"]), bytes.fromhex(str(d["code"])), relocs)  # type: ignore[call-overload]


@dataclass
class DataSpec:
    tu: str
    functions: list[str]
    compile: dict[str, dict[str, object]]  # version -> CompileSpec fields
    offsets: dict[str, dict[str, int]]  # version -> function -> offset from the first
    views: dict[str, dict[str, dict[str, object]]]  # version -> function -> resolved view
    data: dict[str, dict[str, dict[str, object]]]  # version -> section type -> Layout fields
    exports: list[str]  # labels other files use: must stay global, same name and place
    toolchain_exec: str
    objdump: str
    timeout: float
    max_checks: int = 40

    def save(self, control: Path) -> None:
        control.mkdir(parents=True, exist_ok=True)
        (control / DATA_FILE).write_text(json.dumps(asdict(self)), encoding="utf-8")

    @classmethod
    def load(cls, control: Path) -> DataSpec:
        return cls(**json.loads((control / DATA_FILE).read_text(encoding="utf-8")))


def exported_labels(vanilla_tu_text: str) -> list[str]:
    names: list[str] = []
    for m in _EXPORT.finditer(vanilla_tu_text):
        names += [n.strip() for n in m.group(1).split(";")[0].split(",") if n.strip()]
    return list(dict.fromkeys(names))


def prepare_data(s: Session, tu: str, functions: list[str], control: Path) -> DataSpec:
    """Compute the vanilla side for every version and save it in `control`."""
    from rematch.worker.sandbox import _vanilla_lines  # avoid an import cycle

    build = s.definition.require(s.definition.build_info, "build")
    tu_text = (s.worktree / tu).read_text(encoding="utf-8", errors="replace")
    spec = DataSpec(
        tu,
        functions,
        {},
        {},
        {},
        {},
        [],
        s.settings.toolchain_exec,
        s.settings.objdump,
        s.settings.piece_compile_timeout_sec,
        s.settings.max_checks,
    )
    headers = committed_headers(s)  # the functions' landed prototype overrides
    for version in s.target.versions():
        db = CompileDB.load(
            s.definition.build_dir(s.worktree, version) / build.compile_commands, s.worktree
        )
        entry = db.entry(tu)
        spec.compile[version] = compile_spec(s, db.c_template_for(tu), headers)
        vobj = read_object(entry.object_path)
        labels = label_table(s, entry)
        lays = layouts(vobj)
        resolve = locator(lays, labels)
        data_relocs(vobj, lays, resolve)
        spec.data[version] = {
            k: {kk: vv for kk, vv in asdict(v).items() if kk != "base"} for k, v in lays.items()
        }
        for kind, lay_d in spec.data[version].items():
            lay_d["symbols"] = [(o, n, 0) for o, n in labels.get(kind, [])]
        spec.views[version] = {}
        spec.offsets[version] = {}
        first: int | None = None
        for name in functions:
            asm = _function_asm(tu_text, name)
            _, lines = _vanilla_lines(s, entry.object_path, name, asm, entry)
            view = function_view(vobj, name, lines, name, resolve)
            spec.views[version][name] = _view_json(view)
            sym = vobj.function(name)
            assert sym is not None
            first = sym.value if first is None else first
            spec.offsets[version][name] = sym.value - first
        data_labels = {n for kind in DATA_TYPES for _, n in labels.get(kind, [])}
        spec.exports = [n for n in exported_labels(tu_text) if n in data_labels]
    spec.save(control)
    return spec


def _function_asm(tu_text: str, name: str) -> str:
    from rematch.integrate.pret_asm import parse_tu

    return {f.name: f.text for f in parse_tu(tu_text).functions}.get(name, "")


def _compare_layout(kind: str, t: dict[str, object], c: Layout | None) -> list[str]:
    tsize, talign = int(t["size"]), int(t["align"])  # type: ignore[call-overload]
    if c is None:
        return [f"{kind}: missing (target has {tsize:#x} bytes)"] if tsize else []
    out: list[str] = []
    if c.size != tsize:
        out.append(f"{kind}: size {c.size:#x}, target {tsize:#x}")
    if c.align != talign:
        out.append(f"{kind}: starts with alignment {c.align}, target {talign}")
    tdata = bytes.fromhex(str(t["data"]))
    cdata = bytes.fromhex(c.data)
    if tdata != cdata:
        first = next(
            (i for i, (a, b) in enumerate(zip(tdata, cdata, strict=False)) if a != b),
            min(len(tdata), len(cdata)),
        )
        out.append(f"{kind}: bytes differ from offset {first:#x}")
    trel = {tuple(r) for r in t["relocs"]}  # type: ignore[attr-defined]
    crel = {tuple(r) for r in c.relocs}
    if trel != crel:
        out.append(
            f"{kind}: relocations differ (missing {sorted(trel - crel)[:3]},"
            f" extra {sorted(crel - trel)[:3]})"
        )
    if out:
        out.append(f"{kind} layout (target labels | candidate symbols):")
        trows = [(int(o), str(n)) for o, n, _ in t["symbols"]]  # type: ignore[attr-defined]
        crows = [(o, f"{n} ({sz})") for o, n, sz in c.symbols]
        for off in sorted({o for o, _ in trows} | {o for o, _ in crows})[:40]:
            tn = ", ".join(n for o, n in trows if o == off) or "-"
            cn = ", ".join(n for o, n in crows if o == off) or "-"
            out.append(f"    +{off:#06x}  {tn:28} | {cn}")
    return out


def check_data(spec: DataSpec, source: str, work: Path) -> PieceResult:
    runner = ToolchainRunner.from_spec(spec.toolchain_exec)
    result = PieceResult(True)
    worst: dict[str, FunctionStatus] = {}
    for version, cs in spec.compile.items():
        src = work / f"data.{version}.c"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(source, encoding="utf-8")
        compiled = compile_candidate(
            CompileSpec.model_validate(cs).entry(), runner, src, src.with_suffix(".o"), spec.timeout
        )
        if not compiled.ok:
            return PieceResult(False, [f"{version}: {e}" for e in compiled.errors[:12]])
        obj = read_object(compiled.object_path)
        emitted = [f.name for f in obj.functions()]
        if emitted != spec.functions:
            result.errors.append(f"{version}: emits {emitted}, expected {spec.functions}")
        if common := [s.name for s in obj.symbols if s.common]:
            result.errors.append(f"{version}: COMMON symbols {common}; define them (= {{0}})")
        extra = sorted(
            {
                sec.name
                for sec in obj.sections.values()
                if sec.alloc and not sec.name.startswith(".text") and sec.name not in DATA_TYPES
            }
        )
        if extra:
            result.errors.append(f"{version}: unexpected sections {extra}")
        lays = layouts(obj)
        resolve = locator(lays)
        data_relocs(obj, lays, resolve)
        target_data = spec.data[version]
        for kind in sorted(set(target_data) | set(lays)):
            t = target_data.get(
                kind, {"size": 0, "align": 1, "data": "", "relocs": [], "symbols": []}
            )
            result.errors += [
                f"{version}: {line}" for line in _compare_layout(kind, t, lays.get(kind))
            ]
        placed = {n: (k, o) for k, lay in lays.items() for o, n, _ in lay.symbols}
        bind = {s.name: s.bind for s in obj.symbols}
        for label in spec.exports:
            rows = [(k, d["symbols"]) for k, d in target_data.items()]
            want = next(
                ((k, int(o)) for k, syms in rows for o, n, _ in syms if n == label),  # type: ignore[attr-defined]
                None,
            )
            if placed.get(label) != want or bind.get(label) != "GLOBAL":
                result.errors.append(
                    f"{version}: {label} is used by other files: define it"
                    f" global at {want[0] if want else '?'}+"
                    f"{want[1] if want else 0:#x}"
                )
        clines = disassemble(run_objdump(spec.objdump, obj.path), obj)
        offsets = linked_offsets(obj)
        for name in spec.functions:
            if name not in clines or obj.function(name) is None:
                worst[name] = FunctionStatus(name, False, 0.0, [f"{version}: not emitted"])
                continue
            tview = _view(spec.views[version][name])
            cview = function_view(obj, name, clines[name], name, resolve)
            m = binary_match(tview, cview)
            st = FunctionStatus(name, m.match, 100.0 if m.match else 0.0)
            if not m.match:
                st.detail = [f"{version}: {r}" for r in m.reasons]
            if offsets.get(name) != spec.offsets[version][name]:
                st.match = False
                st.detail.insert(
                    0,
                    f"{version}: at +{offsets.get(name, -1):#x}, original"
                    f" +{spec.offsets[version][name]:#x}",
                )
            prev = worst.get(name)
            if prev is None or (prev.match and not st.match):
                worst[name] = st
    result.functions = [worst[n] for n in spec.functions if n in worst]
    result.ok = not result.errors and all(f.match for f in result.functions)
    return result
