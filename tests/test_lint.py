from __future__ import annotations

from rematch.lint.readability import lint, passes


def test_clean_candidate_passes() -> None:
    src = '#include "ctx.h"\n/* goto volatile asm in a comment */\nint f(int x) {\n'
    src += '    const char *s = "goto #pragma";\n    return x + 1;\n}\n'
    r = lint(src)
    assert r.ok and r.flag == [] and passes(r.to_json())


def test_failures_and_flags() -> None:
    src = "\n".join(
        [
            '#include "global.h"',
            "#pragma thumb off",
            "void f(volatile int *p) {",
            "    int a = 1;",
            "    a = a;",
            "    do { *p = 0; } while (0);",
            "    asm { nop }",
            "    *p = (int)(u32)(int)a;",
            "    goto out;",
            "out:;",
            "}",
        ]
    )
    r = lint(src)
    names = [f.rsplit(" (line", 1)[0] for f in r.fail]
    assert names == [
        '#include other than "ctx.h"',
        "inline asm",
        "#pragma",
        "do { } while (0) outside a macro",
        "self-assignment",
    ]
    assert [f.rsplit(" (line", 1)[0] for f in r.flag] == ["goto", "volatile", "cast chain"]
    assert not passes(r.to_json()) and not passes('{"pending": 1}')


def test_member_assignment_is_not_self_assignment() -> None:
    assert lint("void f(S *s, int x) { s->x = x; t.x = x; }").ok
