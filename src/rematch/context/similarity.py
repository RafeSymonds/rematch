"""Function similarity for example selection (SPEC §6.7, Phase 3 B1).

sim(a, b) = 0.6·J(mnemonic 3-grams) + 0.3·J(callees) + 0.1·J(pool constants)

Exact Jaccard over the human-C pool (~10k functions) takes a fraction of a second per target,
so no MinHash approximation is needed. Features for the pool are cached per pin.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from rematch.diff.objdump_backend import Line, disassemble, run_objdump
from rematch.toolchain.objects import read_object

Log = Callable[[str], None]


@dataclass(frozen=True)
class Features:
    grams: frozenset[int]
    callees: frozenset[str]
    constants: frozenset[str]


def features(lines: list[Line]) -> Features:
    mnems = [ln.mnemonic.split(".")[0] for ln in lines if not ln.is_data]
    grams = frozenset(
        zlib.crc32(" ".join(mnems[i : i + 3]).encode()) for i in range(max(0, len(mnems) - 2))
    )
    callees = frozenset(
        ln.reloc
        for ln in lines
        if ln.reloc and ln.mnemonic.split(".")[0] in ("bl", "blx") and not ln.reloc.startswith("<")
    )
    constants = frozenset(ln.operands for ln in lines if ln.mnemonic == ".word")
    return Features(grams, callees, constants)


def _jaccard[T](a: frozenset[T], b: frozenset[T]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def similarity(a: Features, b: Features) -> float:
    return (
        0.6 * _jaccard(a.grams, b.grams)
        + 0.3 * _jaccard(a.callees, b.callees)
        + 0.1 * _jaccard(a.constants, b.constants)
    )


def rank(target: Features, pool: dict[int, Features], exclude: Iterable[int], k: int) -> list[int]:
    """Top `k` pool ids by similarity (ties by id for determinism)."""
    skip = set(exclude)
    scored = [(similarity(target, f), fid) for fid, f in pool.items() if fid not in skip]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [fid for score, fid in scored[:k] if score > 0]


# --- the per-pin feature cache ---------------------------------------------------------


def _encode(f: Features) -> dict[str, object]:
    return {"g": sorted(f.grams), "c": sorted(f.callees), "k": sorted(f.constants)}


def _decode(d: dict[str, list[int | str]]) -> Features:
    return Features(
        frozenset(int(x) for x in d["g"]),
        frozenset(str(x) for x in d["c"]),
        frozenset(str(x) for x in d["k"]),
    )


def build_pool_features(
    objdump: str,
    objects: dict[str, Path],  # tu path -> vanilla object
    functions: dict[int, tuple[str, str]],  # function id -> (tu path, name)
    log: Log,
) -> dict[int, Features]:
    by_tu: dict[str, list[tuple[int, str]]] = {}
    for fid, (tu, name) in functions.items():
        by_tu.setdefault(tu, []).append((fid, name))

    def one(tu: str) -> list[tuple[int, Features]]:
        obj = read_object(objects[tu])
        lines = disassemble(run_objdump(objdump, obj.path), obj)
        return [(fid, features(lines[name])) for fid, name in by_tu[tu] if name in lines]

    out: dict[int, Features] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for part in pool.map(one, [tu for tu in by_tu if tu in objects]):
            out.update(part)
    log(f"similarity features for {len(out)} functions")
    return out


def save_cache(path: Path, key: str, pool: dict[int, Features]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"key": key, "features": {str(fid): _encode(f) for fid, f in pool.items()}}
    path.write_text(json.dumps(data), encoding="utf-8")


def load_cache(path: Path, key: str) -> dict[int, Features] | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("key") != key:
        return None
    return {int(fid): _decode(d) for fid, d in data["features"].items()}
