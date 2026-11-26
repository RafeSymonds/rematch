"""Learning candidates (DESIGN D9): workers propose in NOTES.md; review or curation decides.

Accepted candidates become rules in `rules.py` files (compiler scope in rematch, target scope in
.rematch/). `learnings review` is the human path, `learnings curate` the agent path.
"""

from __future__ import annotations

import sqlite3


def pending(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    rows = conn.execute(
        "SELECT id, text FROM learning_candidates WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    return [(int(i), str(t)) for i, t in rows]


def decide(conn: sqlite3.Connection, cid: int, accept: bool) -> None:
    with conn:
        conn.execute(
            "UPDATE learning_candidates SET status = ? WHERE id = ?",
            ("accepted" if accept else "rejected", cid),
        )
