from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import rematch.learnings.curate_run as cr
from rematch.db import store
from rematch.diff.objdump_backend import Line
from rematch.learnings.curate import decide, pending
from rematch.learnings.rules import Evidence, Rule, RuleFile, sandbox_text
from rematch.learnings.trial import Trial, evidence_holds, modal_entry, run_trial
from rematch.toolchain.compile_db import CompileEntry


def _db(tmp_path: Path, texts: list[str]) -> sqlite3.Connection:
    conn = store.connect(tmp_path / "db.sqlite")
    conn.execute(
        "INSERT INTO runs (id, kind, config_json, started_at) VALUES (1, 'bench', '{}', 't')"
    )
    conn.execute("INSERT INTO attempts (id, run_id, task_kind) VALUES (1, 1, 'function')")
    for text in texts:
        conn.execute(
            "INSERT INTO learning_candidates (attempt_id, text, created_at) VALUES (1, ?, 't')",
            (text,),
        )
    conn.commit()
    return conn


def test_pending_and_decide(tmp_path: Path) -> None:
    conn = _db(tmp_path, ["u8 locals emit lsl/lsr", "swap decl order"])
    (a, _), (b, _) = pending(conn)
    decide(conn, a, accept=True)
    decide(conn, b, accept=False)
    assert pending(conn) == []
    assert cr.pending_count(conn) == 0


def test_rule_file_roundtrip(tmp_path: Path) -> None:
    f = RuleFile.load(tmp_path / "c.json", "Compiler quirks: x")
    assert f.rules == [] and "(No curated rules yet.)" in f.markdown()
    f.rules.append(Rule("C1", "u8 casts emit lsl/lsr #24", Evidence("a", "b", "lsl"), [3], "d"))
    f.rules.append(Rule("C2", "human-accepted", None, [4]))
    f.save()
    g = RuleFile.load(tmp_path / "c.json", "ignored")
    assert g.title == "Compiler quirks: x"
    assert g.rules == f.rules
    assert g.next_id("C") == "C3" and g.next_id("T") == "T1"
    assert (tmp_path / "c.md").read_text() == g.markdown()
    t = RuleFile(tmp_path / "t.json", "Target notes", [Rule("T1", "target rule", None)])
    text = sandbox_text([g, t])
    assert text.index("- u8 casts") < text.index("- target rule")


def _line(text: str) -> Line:
    mnem, _, ops = text.partition(" ")
    return Line(0, mnem, ops)


def test_evidence_holds() -> None:
    a = {"f": [_line("adds r0, r0, #1"), _line("lsls r0, r0, #24"), _line("bx lr")]}
    b = {"f": [_line("adds r0, r0, #1"), _line("bx lr")]}
    assert evidence_holds(Trial(True, [], a, b), r"lsls.*#24") == (True, "ok")
    ok, why = evidence_holds(Trial(True, [], a, b), r"asrs")
    assert not ok and "no differing line" in why
    assert not evidence_holds(Trial(True, [], a, a), "lsls")[0]  # identical code
    assert not evidence_holds(Trial(False, ["a: error"], {}, {}), "x")[0]
    assert "bad expect" in evidence_holds(Trial(True, [], a, b), "(")[1]


def test_run_trial_rejects_other_includes_and_pragmas(tmp_path: Path) -> None:
    compiler = SimpleNamespace()  # never reached
    for src in ('#include "global.h"\n', "#pragma thumb off\n", "#include <stdio.h>\n"):
        t = run_trial(compiler, src, "int x;\n", tmp_path)  # type: ignore[arg-type]
        assert not t.ok and "only #include" in t.errors[0]


def test_modal_entry_picks_most_common_flags() -> None:
    def entry(name: str, *flags: str) -> CompileEntry:
        src = f"../../{name}"
        argv = ("cc", f"-I{name}", *flags, "-o", f"{name}.o", "-c", src)
        return CompileEntry(name, Path("/b"), argv, src, f"{name}.o")

    entries = [
        entry("lib/a.c", "-O4", "-prefix", "x.mch"),
        entry("src/b.c", "-O4", "-W", "error"),
        entry("src/c.c", "-O4", "-W", "error"),
        entry("src/d.s"),
    ]
    assert modal_entry(entries).file == "src/b.c"


def test_names_target() -> None:
    names = {"Widget_GetLevel", "Inventory", "u8"}
    assert cr.names_target("Calling Widget_GetLevel on an Inventory*", names) == [
        "Inventory",
        "Widget_GetLevel",
    ]
    assert cr.names_target("u8 casts emit lsl", names) == []  # short/generic words never count
    names |= {"types", "loop", "data"}  # plain words that also appear in target headers
    assert cr.names_target('#include "types.h"\nint data; /* loop */', names) == []


def test_apply_decisions_verifies_and_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _db(tmp_path, ["u8 cast", "wrong claim", "dup", "names game code", "no evidence"])
    comp = RuleFile(tmp_path / "comp.json", "Compiler", [Rule("C1", "old", None)])
    tgt = RuleFile(tmp_path / "tgt.json", "Target", [])
    monkeypatch.setattr(cr, "rule_files", lambda s: (comp, tgt))
    monkeypatch.setattr(cr, "compiler_for", lambda s: None)
    monkeypatch.setattr(cr, "target_names", lambda s, c: {"Inventory_Get"})
    monkeypatch.setattr(cr, "run_trial", lambda c, a, b, w: (a, b))
    monkeypatch.setattr(
        cr, "evidence_holds", lambda t, e: (True, "ok") if "good" in t[0] else (False, "same code")
    )
    sandbox = tmp_path / "sb"
    (sandbox / "evidence").mkdir(parents=True)
    for name, body in {
        "1a": "good",
        "1b": "x",
        "2a": "bad",
        "2b": "x",
        "4a": "good Inventory_Get",
    }.items():
        (sandbox / "evidence" / f"{name}.c").write_text(body)

    def ev(n: str, m: str) -> dict[str, str]:
        return {"a": f"evidence/{n}.c", "b": f"evidence/{m}.c", "expect": "lsl"}

    decisions: dict[str, object] = {
        "decisions": [
            {
                "candidate": 1,
                "action": "accept",
                "scope": "compiler",
                "text": "u8  cast",
                "evidence": ev("1a", "1b"),
            },
            {
                "candidate": 2,
                "action": "accept",
                "scope": "compiler",
                "text": "x",
                "evidence": ev("2a", "2b"),
            },
            {"candidate": 3, "action": "merge", "into": "C1"},
            {
                "candidate": 4,
                "action": "accept",
                "scope": "compiler",
                "text": "y",
                "evidence": ev("4a", "1b"),
            },
            {"candidate": 5, "action": "accept", "text": "z"},
            {"candidate": 99, "action": "accept"},  # not in this batch: ignored
        ],
        "rewrite": [{"rule": "C1", "text": "new  text"}],
        "retire": [{"rule": "C9"}],  # unknown: ignored
    }
    s = SimpleNamespace(settings=SimpleNamespace(rematch_var=tmp_path / "var"))
    out = cr.apply_decisions(s, conn, sandbox, decisions, {1, 2, 3, 4, 5}, lambda m: None)  # type: ignore[arg-type]

    assert out.accepted == ["C2", "T1"]
    assert out.dropped == [(2, "same code"), (5, "missing evidence")]
    assert out.merged == [3] and out.rewritten == ["C1"] and out.retired == []
    assert out.moved_to_target == ["T1 (Inventory_Get)"]
    assert [r.text for r in comp.rules] == ["new text", "u8 cast"]
    assert tgt.rules[0].sources == [4]
    status = dict(conn.execute("SELECT id, status FROM learning_candidates"))
    assert status == {1: "accepted", 2: "rejected", 3: "accepted", 4: "accepted", 5: "rejected"}
    assert (
        json.loads((tmp_path / "comp.json").read_text())["rules"][1]["evidence"]["expect"] == "lsl"
    )


def test_task_md_gets_the_rules_closest_to_the_target() -> None:
    from rematch.learnings.rules import Rule, RuleFile, selected_text

    rules = [
        Rule("C1", "A `switch` with dense cases emits a jump table (`add pc`).", None),  # type: ignore[arg-type]
        Rule("C2", "Bitfield stores truncate with `lsls` then `lsrs`.", None),  # type: ignore[arg-type]
        Rule("C3", "Loads of `s16` locals use `ldrsh`.", None),  # type: ignore[arg-type]
    ]
    f = RuleFile(Path("x.json"), "Compiler quirks", rules)
    target = "push {r4, lr}\nldrsh r0, [r1, #2]\nadds r0, #1\npop {r4, pc}\n"
    out = selected_text([f], target, 1)
    assert out.startswith("- Loads of `s16` locals use `ldrsh`.")
    assert "2 more rules in LEARNINGS.md" in out
