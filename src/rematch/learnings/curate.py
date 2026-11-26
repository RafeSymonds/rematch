"""Curated learnings (DESIGN D9, CONTRIBUTING): workers propose, a human accepts.

Generic compiler quirks go to rematch's docs/LEARNINGS.md (public, target-agnostic); anything
naming the target's code goes to the target's .rematch/LEARNINGS.md. Sandboxes get both.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MAX_LINES = 150
PLACEHOLDER = "(No curated rules yet.)"


def pending(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    rows = conn.execute(
        "SELECT id, text FROM learning_candidates WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    return [(int(i), str(t)) for i, t in rows]


def append_rule(path: Path, text: str) -> bool:
    """Add `- text` to a LEARNINGS file unless it's already there. Returns whether it was added."""
    rule = f"- {' '.join(text.split())}"
    body = path.read_text(encoding="utf-8") if path.is_file() else "# LEARNINGS\n"
    if rule in body.splitlines():
        return False
    body = body.replace(PLACEHOLDER + "\n", "").rstrip("\n") + "\n" + rule + "\n"
    path.write_text(body, encoding="utf-8")
    return True


def decide(conn: sqlite3.Connection, cid: int, accept: bool) -> None:
    with conn:
        conn.execute(
            "UPDATE learning_candidates SET status = ? WHERE id = ?",
            ("accepted" if accept else "rejected", cid),
        )


def line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else 0


def combined(generic: Path, target: Path) -> str:
    """What a sandbox's LEARNINGS.md holds: the generic rules, then the target's."""
    parts = [generic.read_text(encoding="utf-8").strip()] if generic.is_file() else []
    if target.is_file():
        parts.append(target.read_text(encoding="utf-8").strip())
    return "\n\n".join(parts) + "\n"


def stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
