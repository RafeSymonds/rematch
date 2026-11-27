"""type_merge clustering: used fields by variable, evidence edges, conflict-checked merging."""

from __future__ import annotations

from rematch.types.merge import TypeUse, analyze, cluster, evidence

A = """/* rematch:types */
typedef struct UnkStruct_02000000_1 { u8 unk_00[0x4]; u16 unk_04; } UnkStruct_02000000_1;
typedef struct UnkStruct_02000000 {
    u8 unk_00[0x10]; UnkStruct_02000000_1 *unk_10; int unk_14;
} UnkStruct_02000000;
/* rematch:end-types */
void FuncB(UnkStruct_02000000 *p);
void FuncA(UnkStruct_02000000 *work) {
    work->unk_14 = work->unk_10->unk_04;
    FuncB(work);
}
"""
B = """/* rematch:types */
typedef struct UnkStruct_02000020 { u8 unk_00[0x14]; int unk_14; u32 unk_18; } UnkStruct_02000020;
/* rematch:end-types */
void FuncB(UnkStruct_02000020 *p) {
    p->unk_18 = p->unk_14;
}
"""
C = """/* rematch:types */
typedef struct UnkStruct_02000040 { u8 unk_00[0x14]; u16 unk_14; } UnkStruct_02000040;
/* rematch:end-types */
void FuncC(UnkStruct_02000040 *q) {
    FuncB((void *)q);
    q->unk_14 = sizeof(UnkStruct_02000040);
}
"""

FIELDS = {
    "UnkStruct_02000000_1": [("unk_00", 0, 4, "u8 unk_00[0x4]"), ("unk_04", 4, 2, "u16 unk_04")],
    "UnkStruct_02000000": [
        ("unk_00", 0, 16, "u8 unk_00[0x10]"),
        ("unk_10", 16, 4, "UnkStruct_02000000_1 *unk_10"),
        ("unk_14", 20, 4, "int unk_14"),
    ],
    "UnkStruct_02000020": [
        ("unk_00", 0, 20, "u8 unk_00[0x14]"),
        ("unk_14", 20, 4, "int unk_14"),
        ("unk_18", 24, 4, "u32 unk_18"),
    ],
    "UnkStruct_02000040": [("unk_00", 0, 20, "u8 unk_00[0x14]"), ("unk_14", 20, 2, "u16 unk_14")],
}


def info(name: str) -> dict[str, object]:
    return {
        "fields": [{"name": n, "offset": o, "size": s, "decl": d} for n, o, s, d in FIELDS[name]]
    }


def uses() -> list[TypeUse]:
    out = []
    for fid, (fname, src, names) in enumerate(
        [
            ("FuncA", A, ["UnkStruct_02000000_1", "UnkStruct_02000000"]),
            ("FuncB", B, ["UnkStruct_02000020"]),
            ("FuncC", C, ["UnkStruct_02000040"]),
        ]
    ):
        facts = analyze(src, {n: info(n) for n in names})
        for n in names:
            used, bound = facts[n]
            size = max(o + s for _, o, s, _ in FIELDS[n])
            out.append(TypeUse(fid, fname, "ov1", n, size, info(n)["fields"], used, bound))  # type: ignore[arg-type]
    return out


def test_used_fields_follow_variables_and_pointer_chains() -> None:
    by = {u.name: u for u in uses()}
    assert by["UnkStruct_02000000"].used == {"unk_10", "unk_14"}
    assert by["UnkStruct_02000000_1"].used == {"unk_04"}  # through work->unk_10->
    assert "unk_00" not in by["UnkStruct_02000020"].used  # padding is not a use
    assert by["UnkStruct_02000040"].size_bound and not by["UnkStruct_02000000"].size_bound


def test_call_and_proto_edges_merge_compatible_types_and_refuse_conflicts() -> None:
    us = uses()
    sources = {0: ("FuncA", A), 1: ("FuncB", B), 2: ("FuncC", C)}
    edges = evidence(us, sources)
    kinds = {(a[1], b[1], k) for a, b, k in edges}
    assert ("UnkStruct_02000000", "UnkStruct_02000020", "call") in kinds
    assert ("UnkStruct_02000000", "UnkStruct_02000020", "proto") in kinds
    clusters, refused = cluster(us, edges)
    merged = [sorted(n for _, n in c.members) for c in clusters if len(c.members) > 1]
    assert merged == [["UnkStruct_02000000", "UnkStruct_02000020"]]
    # FuncC passes q (u16 at 0x14) where FuncB reads an int at 0x14: a real conflict
    assert ("UnkStruct_02000040", "UnkStruct_02000020", "call") in kinds
    assert refused >= 1
