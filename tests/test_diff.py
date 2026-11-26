"""Score, alignment, hint rules (one fixture each), rendering, error tidying, pre-filter."""

from __future__ import annotations

import pytest

from rematch.diff import hints as H
from rematch.diff.objdump_backend import Line
from rematch.diff.render import (
    MAX_LINES,
    VersionResult,
    display_score,
    render_diff,
    render_result,
)
from rematch.diff.score import align, score
from rematch.toolchain.compile import tidy_errors
from rematch.worker.check_entry import keep_reference, prefilter


def L(text: str, off: int = 0, **kw: object) -> Line:
    mn, _, ops = text.partition(" ")
    return Line(off, mn, ops.strip(), **kw)  # type: ignore[arg-type]


def lines(*texts: str) -> list[Line]:
    return [L(t, i * 2) for i, t in enumerate(texts)]


# --- alignment and score ---------------------------------------------------------------


def test_identical_scores_100() -> None:
    a = lines("push {r4, lr}", "movs r0, #1", "pop {r4, pc}")
    ops = align(a, a)
    assert [op.kind for op in ops] == ["equal"] * 3
    assert score(ops, 3, 3) == 100.0


def test_costs() -> None:
    t = lines("push {r4, lr}", "adds r0, r4, r1", "bhs .L0", "pop {r4, pc}")
    c = lines("push {r4, lr}", "adds r0, r1, r4", "bge .L0", "lsls r0, r0, #24", "pop {r4, pc}")
    ops = align(t, c)
    kinds = [op.kind for op in ops]
    assert kinds.count("operands") == 1 and kinds.count("opcode") == 1
    assert kinds.count("extra") == 1
    # cost 0.5 + 1 + 1 over max(4, 5)
    assert score(ops, 4, 5) == pytest.approx(100 * (1 - 2.5 / 5))


def test_missing_and_empty() -> None:
    t = lines("movs r0, #1", "bx lr")
    ops = align(t, lines("bx lr"))
    assert [op.kind for op in ops] == ["missing", "equal"]
    assert score(align([], []), 0, 0) == 100.0


# --- hint rules: one fixture per rule ----------------------------------------------------


def test_hint_width_mismatch() -> None:
    t = lines("ldrb r0, [r1, #0]", "bx lr")
    c = lines("ldrb r0, [r1, #0]", "lsls r0, r0, #24", "asrs r0, r0, #24", "bx lr")
    hint = H.width_mismatch(align(t, c))
    assert hint is not None and "signed" in hint and "your code" in hint
    assert H.width_mismatch(align(t, t)) is None


def test_hint_signedness() -> None:
    t = lines("cmp r0, #5", "bhs.n .L0", "bx lr")
    c = lines("cmp r0, #5", "bge.n .L0", "bx lr")
    assert H.signedness(align(t, c)) is not None
    assert H.signedness(align(t, lines("cmp r0, #5", "bne.n .L0", "bx lr"))) is None


def test_hint_register_permutation() -> None:
    t = lines("adds r0, r4, r1", "movs r2, r5")
    c = lines("adds r0, r1, r4", "movs r2, r5")
    assert H.register_permutation(align(t, c)) is not None
    assert H.register_permutation(align(t, lines("adds r0, r4, #1", "movs r2, r5"))) is None


def test_hint_push_pop_sets() -> None:
    t = lines("push {r3, r4, r5, lr}", "bx lr")
    c = lines("push {r3, r4, lr}", "bx lr")
    assert H.push_pop_sets(align(t, c)) is not None
    assert H.push_pop_sets(align(t, t)) is None


def test_hint_jump_table() -> None:
    t = lines("cmp r0, #9", "add pc, r0", "bx lr")
    c = lines("cmp r0, #9", "beq.n .L0", "bx lr")
    assert "original uses a jump table" in (H.jump_table(align(t, c)) or "")
    assert "yours uses a jump table" in (H.jump_table(align(c, t)) or "")
    assert H.jump_table(align(t, t)) is None


def test_hint_inline_vs_call() -> None:
    t = [L("bl Foo", 0, reloc="Foo"), L("bx lr", 4)]
    c = [L("movs r0, #1", 0), L("adds r0, #2", 2), L("bx lr", 4)]
    assert "original calls Foo" in (H.inline_vs_call(align(t, c)) or "")
    assert "yours calls Foo" in (H.inline_vs_call(align(c, t)) or "")


def test_hint_pool_value() -> None:
    t = lines("ldr r0, =0x1f4", "bx lr")
    c = lines("ldr r0, =0x1f5", "bx lr")
    assert "0x1f4 vs 0x1f5" in (H.pool_value(align(t, c)) or "")
    assert H.pool_value(align(t, lines("ldr r0, [r1, #4]", "bx lr"))) is None


def test_hints_by_mode() -> None:
    t = lines("cmp r0, #5", "bhs.n .L0")
    c = lines("cmp r0, #5", "bge.n .L0")
    assert [rule for rule, _ in H.hints(align(t, c), "thumb")] == ["signedness"]
    assert H.hints(align(t, c), "mips") == []


# --- rendering ------------------------------------------------------------------------


def result(t: list[Line], c: list[Line], match: bool = False) -> VersionResult:
    ops = align(t, c)
    return VersionResult(
        "v1", match, score(ops, len(t), len(c)), len(t), len(c), ops, H.hints(ops, "thumb")
    )


def test_render_match_and_99_9() -> None:
    t = lines("bx lr")
    assert render_result(1, 40, 100.0, 100.0, [result(t, t, match=True)]).startswith("MATCH")
    r = result(t, t, match=False)
    assert display_score(r) == 99.9


def test_render_collapses_and_caps() -> None:
    t = lines(*[f"movs r0, #{i}" for i in range(200)])
    c = list(t)
    c[100] = L("movs r0, #999", 200)
    body = render_diff(align(t, c))
    assert body[0] == "(99 matching lines collapsed)"
    assert any("~operands" in row for row in body)
    assert body[-1] == "(98 matching lines collapsed)"  # one context line on each side
    many = lines(*[f"adds r{i % 8}, #{i}" for i in range(300)])
    text = render_result(1, 40, 10.0, 10.0, [result(many, lines("bx lr"))])
    assert len(text.splitlines()) <= MAX_LINES
    assert text.splitlines()[0].startswith("check 1/40 | score")


# --- compiler errors and pre-filter ---------------------------------------------------


def test_tidy_errors_maps_scratch_lines() -> None:
    out = (
        "\\Users\\x\\work\\scratch.v1.c:25: expression syntax error\n"
        "\\Users\\x\\work\\candidate.c:23: note:     (corresponding #line reference)\n"
        "Errors caused tool to abort.\n"
    )
    assert tidy_errors(out) == ("candidate.c:23: expression syntax error",)


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        ("int F(void) { asm { nop } return 0; }", "inline asm"),
        ('int F(void) { __asm("nop"); return 0; }', "inline asm"),
        ("#pragma thumb off\nint F(void) { return 0; }", "#pragma"),
        ('#include "other.h"\nint F(void) { return 0; }', "#include"),
        ("int G(void) { return 0; }", "does not define"),
        ("int F(void) { return 0; }\nint F(void) { return 1; }", "2 times"),
    ],
)
def test_prefilter_rejects(candidate: str, reason: str) -> None:
    assert reason in (prefilter(candidate, "F") or "")


@pytest.mark.parametrize(
    "candidate",
    [
        "int F(void) { return 0; }",
        '#include "ctx.h"\nint F(void) { return 0; }',
        "// asm and #pragma in a comment\nint F(void) { return 0; }",
        'int F(void) { const char *s = "asm"; return s[0]; }',
    ],
)
def test_prefilter_accepts(candidate: str) -> None:
    assert prefilter(candidate, "F") is None


def test_keep_reference_only_for_static() -> None:
    assert "rematch_keep_F" in keep_reference("static int F(void) { return 0; }", "F")
    assert keep_reference("int F(void) { return 0; }", "F") == ""
