from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rematch.db import store

TABLES = {"functions", "runs", "attempts", "matches", "learning_candidates", "bench_sets"}


def schema_of(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {(name, " ".join(sql.split())) for name, sql in rows}


def test_fresh_database_reaches_latest(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "sub" / "db.sqlite")
    assert store.schema_version(conn) == store.latest_version() >= 1
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names >= TABLES
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_reconnect_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store.connect(path).close()
    conn = store.connect(path)
    assert store.migrate(conn) == 0
    assert store.schema_version(conn) == store.latest_version()


def test_migrations_equal_schema_sql() -> None:
    migrated = sqlite3.connect(":memory:")
    store.migrate(migrated)
    reference = sqlite3.connect(":memory:")
    reference.executescript(store.SCHEMA_SQL.read_text())
    assert schema_of(migrated) == schema_of(reference)


def test_newer_database_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {store.latest_version() + 1}")
    conn.close()
    with pytest.raises(store.MigrationError, match="newer"):
        store.connect(path)


def test_schema_constraints(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite")
    insert = (
        "INSERT INTO functions (name, address, module, tu_path, source_kind, mode, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    conn.execute(insert, ("f", 0x1000, "main", "asm/a.s", "asm", "mips", "t"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("g", 0x1000, "main", "asm/a.s", "asm", "thumb", "t"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("h", 0x2000, "main", "asm/a.s", "rust", "thumb", "t"))
