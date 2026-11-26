"""SQLite store: connect and apply forward-only migrations (SPEC §5)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
SCHEMA_SQL = Path(__file__).parent / "schema.sql"
_MIGRATION_NAME = re.compile(r"^(\d{4})_[A-Za-z0-9_]+\.sql$")


class MigrationError(Exception):
    pass


def migrations() -> list[tuple[int, Path]]:
    """Migration files as (number, path), numbered 1..N with no gaps."""
    found: list[tuple[int, Path]] = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        match = _MIGRATION_NAME.match(path.name)
        if match is None:
            raise MigrationError(f"bad migration file name: {path.name}")
        found.append((int(match.group(1)), path))
    found.sort()
    numbers = [n for n, _ in found]
    if numbers != list(range(1, len(found) + 1)):
        raise MigrationError(f"migrations must be numbered 1..N without gaps, got {numbers}")
    return found


def latest_version() -> int:
    return len(migrations())


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations, each in its own transaction. Returns how many ran.

    Foreign keys are off while a migration runs, so it can rebuild a referenced table (SQLite
    cannot alter constraints in place). They are checked before commit, and any violation
    rolls the migration back.
    """
    current = schema_version(conn)
    available = migrations()
    if current > len(available):
        raise MigrationError(
            f"database is at version {current}, newer than this rematch ({len(available)})"
        )
    fk_enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    applied = 0
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        for number, path in available[current:]:
            sql = path.read_text(encoding="utf-8")
            try:
                conn.executescript(f"BEGIN;\n{sql}\n")
                problems = conn.execute("PRAGMA foreign_key_check").fetchall()
                if problems:
                    raise MigrationError(f"{path.name}: foreign key violations {problems[:5]}")
                conn.execute(f"PRAGMA user_version = {number}")
                conn.execute("COMMIT")
            except (sqlite3.Error, MigrationError):
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            applied += 1
    finally:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if fk_enabled else 'OFF'}")
    return applied


def connect(path: Path) -> sqlite3.Connection:
    """Open the database at `path`, creating it if needed, and bring it to the latest schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn
