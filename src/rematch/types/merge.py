"""`type_merge` clustering (SPEC §16.2, Phase 4b): which provisional types are the same type.

Provisional types are per match. Within a module (pret TU splits are arbitrary, modules are
not), evidence links two of them:
- call: a match passes a variable of its type to a callee whose definition takes that
  parameter as its own type, or casts the parameter to it;
- proto: a match's prototype of a callee uses its type where the callee's definition uses
  another provisional type (parameters and return value);
- extern: two matches declare the same extern object with their own types;
- field: two linked types have pointer fields at the same offset to provisional types;
- layout (opt-in): three or more used fields agree in offset and size.

Merging is greedy and checks conflicts at every step, on the fields a body actually uses
(padding arrays are measured as fields, so declared layouts alone would conflict almost
always). A type used in a size-dependent way (sizeof, copies, indexing, pointer arithmetic,
embedding) merges only with types of exactly its size. Types with bitfields are left out: the
probe does not measure them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

from rematch.integrate.compose import definition_header
from rematch.inventory.c_parser import strip_comments
from rematch.types.provisional import TypesError, extract_block

Key = tuple[int, str]  # (function id, type name)
_PROV = r"Unk(?:Struct|Union)_[0-9A-F]{8}(?:_\d+)?"
_PTR_FIELD = re.compile(rf"\b({_PROV})\s*\*")
_COPY = re.compile(r"\b(?:memcpy|memset|memmove|MI_Cpu\w*|MI_Copy\w*)\s*\(([^;]*)\)")


@dataclass
class TypeUse:
    fid: int
    function: str
    module: str
    name: str
    size: int
    fields: list[dict[str, object]]  # measured: name, offset, size, decl
    used: set[str] = field(default_factory=set)
    size_bound: bool = False
    bitfield: bool = False

    @property
    def key(self) -> Key:
        return (self.fid, self.name)

    def used_fields(self) -> list[tuple[int, int, str]]:
        return sorted(
            (int(f["offset"]), int(f["size"]), str(f["name"]))  # type: ignore[call-overload]
            for f in self.fields
            if f["name"] in self.used
        )

    def pointer_fields(self) -> dict[int, str]:
        """offset -> provisional type the field points to."""
        out: dict[int, str] = {}
        for f in self.fields:
            m = _PTR_FIELD.search(str(f.get("decl", "")))
            if m:
                out[int(f["offset"])] = m.group(1)  # type: ignore[call-overload]
        return out


def _body(source: str) -> str:
    """The source without its types block, comments or string contents."""
    try:
        body = extract_block(source)
    except TypesError:
        body = None
    text = source
    if body is not None:
        text = source.replace(body, "")
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', strip_comments(text))


def _pointer_vars(body: str, type_name: str) -> set[str]:
    return set(re.findall(rf"\b{type_name}\s*\*+\s*(?:const\s+)?(\w+)", body))


_CHAIN = re.compile(
    r"(?:\s*\[[^\]]*\])*((?:\s*(?:->|\.)\s*unk_[0-9A-Fa-f]+(?:_\w+)?(?:\s*\[[^\]]*\])*)+)"
)
_SEG = re.compile(r"(?:->|\.)\s*(unk_[0-9A-Fa-f]+(?:_\w+)?)")
_EMBED = re.compile(rf"\b({_PROV})\s+\**\s*unk_")


def _field_type(info: dict[str, object], name: str) -> str | None:
    """The provisional type a field points to or embeds."""
    for f in info["fields"]:  # type: ignore[attr-defined]
        if f["name"] == name:
            m = _PTR_FIELD.search(str(f.get("decl", ""))) or _EMBED.search(str(f.get("decl", "")))
            return m.group(1) if m else None
    return None


def analyze(source: str, types: dict[str, dict[str, object]]) -> dict[str, tuple[set[str], bool]]:
    """type name -> (field names the body accesses through a variable or cast of that type,
    size-bound). Chains follow pointer and embedded fields (`p->unk_14->unk_04`)."""
    body = _body(source)
    block = extract_block(source) or ""
    used: dict[str, set[str]] = {name: set() for name in types}

    def walk(start: str, chain: str) -> None:
        current: str | None = start
        for seg in _SEG.findall(chain):
            if current is None or current not in types:
                return
            used[current].add(seg)
            current = _field_type(types[current], seg)

    for name in types:
        roots = set(re.findall(rf"\b{name}\s*\**\s*(?:const\s+)?(\w+)\s*[;,=)\[]", body))
        for var in roots:
            for m in re.finditer(rf"\b{var}\b", body):
                chain = _CHAIN.match(body, m.end())
                if chain:
                    walk(name, chain.group(1))
        for m in re.finditer(rf"\(\s*\(\s*{name}\s*\*\s*\)\s*[^()]*\)", body):
            chain = _CHAIN.match(body, m.end())
            if chain:
                walk(name, chain.group(1))
    out: dict[str, tuple[set[str], bool]] = {}
    for name in types:
        bound = bool(re.search(rf"sizeof\s*\(\s*(?:struct\s+)?{name}\s*\)", body))
        bound |= bool(re.search(rf"\b{name}\s+\w+\s*[;=\[]", body))  # by value
        bound |= bool(re.search(rf"\b{name}\s+unk_\w+\s*[;\[]", block))  # embedded, arrays
        for v in _pointer_vars(body, name):
            if re.search(
                rf"sizeof\s*\(\s*\*\s*{v}\s*\)|\b{v}\s*\[|\b{v}\s*(?:\+\+|--)|(?:\+\+|--)\s*{v}\b"
                rf"|\b{v}\s*[+-]\s*(?!>)\w|\*\s*{v}\s*=(?!=)",
                body,
            ):
                bound = True
            if any(re.search(rf"\b{v}\b", m) for m in _COPY.findall(body)):
                bound = True
        out[name] = (used[name], bound)
    return out


def load_uses(conn: sqlite3.Connection, module: str | None = None) -> list[TypeUse]:
    query = (
        "SELECT p.function_id, f.name, f.module, p.name, p.size, p.fields_json, p.source,"
        " m.c_source FROM provisional_types p JOIN functions f ON f.id = p.function_id"
        " JOIN matches m ON m.function_id = p.function_id"
        " WHERE f.status IN ('matched', 'integrated') AND p.shared_type_id IS NULL"
    )
    args: list[object] = []
    if module:
        query += " AND f.module = ?"
        args.append(module)
    rows = conn.execute(query, args).fetchall()
    by_fid: dict[int, list[tuple]] = {}  # type: ignore[type-arg]
    for row in rows:
        by_fid.setdefault(int(row[0]), []).append(row)
    out: list[TypeUse] = []
    for fid, items in by_fid.items():
        source = str(items[0][7])
        infos = {str(r[3]): {"fields": json.loads(r[5])} for r in items}
        try:
            facts = analyze(source, infos)
        except TypesError:
            continue
        for _fid, fname, mod, name, size, fields_json, decl, _src in items:
            used, bound = facts.get(str(name), (set(), False))
            out.append(
                TypeUse(
                    fid,
                    str(fname),
                    str(mod),
                    str(name),
                    int(size or 0),
                    json.loads(fields_json),
                    used,
                    bound,
                    ":" in re.sub(r"\[[^\]]*\]", "", str(decl).split("{", 1)[-1]),
                )
            )
    return out


# --- evidence ----------------------------------------------------------------------------


def _split_args(text: str) -> list[str]:
    out, depth, start = [], 0, 0
    for i, c in enumerate(text):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(text[start:i].strip())
            start = i + 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _call_args(body: str, callee: str) -> list[list[str]]:
    calls = []
    for m in re.finditer(rf"\b{re.escape(callee)}\s*\(", body):
        depth, i = 1, m.end()
        while i < len(body) and depth:
            depth += {"(": 1, ")": -1}.get(body[i], 0)
            i += 1
        calls.append(_split_args(body[m.end() : i - 1]))
    return calls


@dataclass
class Signature:
    ret: str | None  # provisional type the function returns a pointer to
    params: list[str | None]  # per parameter: its provisional type, declared or cast to


def _prov_in(text: str) -> str | None:
    m = re.search(rf"\b({_PROV})\s*\*", text)
    return m.group(1) if m else None


def signature(name: str, source: str) -> Signature | None:
    header = definition_header(name, source)
    if header is None:
        return None
    head, _, args = header.partition("(")
    params: list[str | None] = []
    body = _body(source)
    for arg in _split_args(args.rstrip(")")):
        declared = _prov_in(arg)
        pname = re.findall(r"(\w+)\s*(?:\[\s*\])?\s*$", arg)
        if declared is None and pname:
            cast = re.search(rf"\(\s*({_PROV})\s*\*\s*\)\s*{pname[0]}\b", body)
            declared = cast.group(1) if cast else None
        params.append(declared)
    return Signature(_prov_in(head), params)


def evidence(
    uses: list[TypeUse], sources: dict[int, tuple[str, str]], layout: bool = False
) -> list[tuple[Key, Key, str]]:
    """Edges between provisional types. `sources`: function id -> (name, c_source)."""
    have = {u.key for u in uses}
    by_name = {name: fid for fid, (name, _) in sources.items()}
    sigs = {fid: signature(name, src) for fid, (name, src) in sources.items()}
    edges: list[tuple[Key, Key, str]] = []

    def add(a: Key, b: Key, kind: str) -> None:
        if a in have and b in have and a != b:
            edges.append((a, b, kind))

    externs: dict[str, list[Key]] = {}
    for fid, (_fname, src) in sources.items():
        body = _body(src)
        for callee, cfid in by_name.items():
            if cfid == fid or sigs.get(cfid) is None or not re.search(rf"\b{callee}\s*\(", body):
                continue
            csig = sigs[cfid]
            assert csig is not None
            for args in _call_args(body, callee):
                for i, arg in enumerate(args[: len(csig.params)]):
                    want = csig.params[i]
                    if want is None:
                        continue
                    mine = _prov_in(arg)
                    bare = re.sub(r"^\(\s*(?:const\s+)?\w+\s*\*+\s*\)\s*", "", arg)  # (void *)x
                    if mine is None and re.fullmatch(r"&?\w+", bare):
                        var = bare.lstrip("&")
                        decl = re.search(rf"\b({_PROV})\s*\*?\s*{var}\b", body)
                        mine = decl.group(1) if decl else None
                    if mine:
                        add((fid, mine), (cfid, want), "call")
            proto = re.search(rf"^[^;{{}}\n]*\b{callee}\s*\(([^;{{}}]*)\)\s*;", body, re.M)
            if proto:
                ret_mine = _prov_in(proto.group(0).split(callee)[0])
                if ret_mine and csig.ret:
                    add((fid, ret_mine), (cfid, csig.ret), "proto")
                for i, arg in enumerate(_split_args(proto.group(1))[: len(csig.params)]):
                    mine, want = _prov_in(arg), csig.params[i]
                    if mine and want:
                        add((fid, mine), (cfid, want), "proto")
        for m in re.finditer(rf"\bextern\s+(?:const\s+)?({_PROV})\s*\*?\s*(\w+)", body):
            externs.setdefault(m.group(2), []).append((fid, m.group(1)))
    for keys in externs.values():
        for k in keys[1:]:
            add(keys[0], k, "extern")
    if layout:
        sig = {u.key: set(u.used_fields()) for u in uses}
        items = [u for u in uses if len(sig[u.key]) >= 3]
        for i, a in enumerate(items):
            for b in items[i + 1 :]:
                if a.module == b.module and len(sig[a.key] & sig[b.key]) >= 3:
                    add(a.key, b.key, "layout")
    return edges


# --- clustering --------------------------------------------------------------------------


@dataclass
class Cluster:
    members: list[Key]
    used: dict[int, int]  # offset -> size, union of used fields
    size_bound: int | None  # the exact size, when any member is size-bound
    sizes: set[int]


def _conflict(c: Cluster, d: Cluster) -> str | None:
    for off, size in d.used.items():
        if off in c.used and c.used[off] != size:
            return f"field +{off:#x}: size {c.used[off]} vs {size}"
        for o2, s2 in c.used.items():
            if o2 != off and o2 < off + size and off < o2 + s2:
                return f"fields +{o2:#x} and +{off:#x} overlap"
    bound = {b for b in (c.size_bound, d.size_bound) if b is not None}
    if len(bound) > 1:
        return f"size-bound sizes {sorted(bound)}"
    if bound:
        size = next(iter(bound))
        if any(s != size for s in c.sizes | d.sizes):
            return f"size-bound size {size} vs {sorted(c.sizes | d.sizes)}"
    return None


def cluster(
    uses: list[TypeUse], edges: Iterable[tuple[Key, Key, str]]
) -> tuple[list[Cluster], int]:
    """Greedy merging along edges, most specific evidence first. Returns (clusters, number of
    merges refused for conflicts)."""
    by_key = {u.key: u for u in uses if not u.bitfield}
    of: dict[Key, Cluster] = {}
    for u in by_key.values():
        used = {o: s for o, s, _ in u.used_fields()}
        of[u.key] = Cluster([u.key], used, u.size if u.size_bound else None, {u.size})
    order = {"call": 0, "proto": 1, "extern": 2, "field": 3, "layout": 4}
    queue = sorted(edges, key=lambda e: order.get(e[2], 9))
    refused = 0
    while queue:
        a, b, _kind = queue.pop(0)
        if a not in of or b not in of or of[a] is of[b]:
            continue
        c, d = of[a], of[b]
        if _conflict(c, d) is not None:
            refused += 1
            continue
        c.members += d.members
        c.used.update(d.used)
        c.size_bound = c.size_bound if c.size_bound is not None else d.size_bound
        c.sizes |= d.sizes
        for k in d.members:
            of[k] = c
        # field propagation: pointer fields at the same offset point to the same type
        for x in c.members:
            for y in c.members:
                if x >= y:
                    continue
                px, py = by_key[x].pointer_fields(), by_key[y].pointer_fields()
                for off in px.keys() & py.keys():
                    kx, ky = (x[0], px[off]), (y[0], py[off])
                    if kx in of and ky in of and of[kx] is not of[ky]:
                        queue.append((kx, ky, "field"))
    unique = {id(c): c for c in of.values()}
    return list(unique.values()), refused


def stats(clusters: list[Cluster], refused: int, edges: list[tuple[Key, Key, str]]) -> str:
    multi = [c for c in clusters if len(c.members) > 1]
    kinds: dict[str, int] = {}
    for _, _, k in edges:
        kinds[k] = kinds.get(k, 0) + 1
    sizes = sorted((len(c.members) for c in multi), reverse=True)
    return (
        f"{sum(len(c.members) for c in clusters)} types, edges {kinds}, "
        f"{len(multi)} clusters of 2+ covering {sum(sizes)} types "
        f"(largest {sizes[:5]}), {refused} merges refused for conflicts"
    )
