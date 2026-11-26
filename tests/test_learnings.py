from __future__ import annotations

from pathlib import Path

from rematch.db import store
from rematch.learnings.curate import PLACEHOLDER, append_rule, combined, decide, pending


def test_review_flow(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite")
    conn.execute(
        "INSERT INTO runs (id, kind, config_json, started_at) VALUES (1, 'bench', '{}', 't')"
    )
    conn.execute("INSERT INTO attempts (id, run_id, task_kind) VALUES (1, 1, 'function')")
    for text in ("u8 locals compared to constants emit lsl/lsr", "swap decl order to swap slots"):
        conn.execute(
            "INSERT INTO learning_candidates (attempt_id, text, created_at) VALUES (1, ?, 't')",
            (text,),
        )
    conn.commit()
    (a, ta), (b, _) = pending(conn)
    generic = tmp_path / "LEARNINGS.md"
    generic.write_text(f"# LEARNINGS\n\n{PLACEHOLDER}\n")
    assert append_rule(generic, ta) and not append_rule(generic, ta)  # deduplicated
    decide(conn, a, accept=True)
    decide(conn, b, accept=False)
    assert pending(conn) == []
    assert PLACEHOLDER not in generic.read_text()
    assert generic.read_text().endswith("- u8 locals compared to constants emit lsl/lsr\n")
    target = tmp_path / "target.md"
    target.write_text("# Target LEARNINGS\n- one\n")
    both = combined(generic, target)
    assert both.index("u8 locals") < both.index("- one")
    assert combined(generic, tmp_path / "missing.md").strip() == generic.read_text().strip()
