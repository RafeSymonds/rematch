from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rematch.db import store

TABLES = {
    "functions",
    "runs",
    "attempts",
    "matches",
    "learning_candidates",
    "bench_sets",
    "shared_types",
    "provisional_types",
    "type_uses",
    "name_proposals",
}


def schema_of(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    # A rebuilt-and-renamed table is stored as CREATE TABLE "name"; quoting is not schema.
    return {(name, " ".join(sql.replace('"', "").split())) for name, sql in rows}


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


def _apply_until(conn: sqlite3.Connection, version: int) -> None:
    for number, path in store.migrations()[:version]:
        conn.executescript(f"BEGIN;\n{path.read_text()}\nPRAGMA user_version = {number};\nCOMMIT;")


def test_0002_keeps_data_and_foreign_keys(tmp_path: Path) -> None:
    """Upgrading a populated v1 database rebuilds attempts without losing rows or references."""
    path = tmp_path / "db.sqlite"
    conn = sqlite3.connect(path)
    _apply_until(conn, 1)
    conn.executescript(
        """
        INSERT INTO functions (id, name, address, module, tu_path, source_kind, mode, updated_at)
            VALUES (1, 'f', 4096, 'main', 'asm/a.s', 'asm', 'thumb', 't');
        INSERT INTO runs (id, kind, config_json, started_at) VALUES (1, 'live', '{}', 't');
        INSERT INTO attempts (id, run_id, function_id, task_kind) VALUES (7, 1, 1, 'function');
        INSERT INTO matches (function_id, attempt_id, c_source, verified_versions, lint_json,
            verified_at) VALUES (1, 7, 'int f(void);', 'v', '{}', 't');
        """
    )
    conn.commit()
    conn.close()

    conn = store.connect(path)
    assert store.schema_version(conn) == store.latest_version()
    assert conn.execute("SELECT id, task_kind FROM attempts").fetchall() == [(7, "function")]
    assert conn.execute("SELECT attempt_id FROM matches").fetchall() == [(7,)]
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.execute("INSERT INTO attempts (run_id, task_kind) VALUES (1, 'type_merge')")
    conn.execute("INSERT INTO attempts (run_id, task_kind) VALUES (1, 'naming')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO attempts (run_id, task_kind) VALUES (1, 'other')")
    with pytest.raises(sqlite3.IntegrityError):  # foreign keys enforced again after migrating
        conn.execute(
            "INSERT INTO matches (function_id, attempt_id, c_source, verified_versions, "
            "lint_json, verified_at) VALUES (1, 999, '', 'v', '{}', 't')"
        )


def test_type_recovery_tables(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite")
    conn.executescript(
        """
        INSERT INTO functions (id, name, address, module, tu_path, source_kind, mode, updated_at)
            VALUES (1, 'f', 4096, 'main', 'asm/a.s', 'asm', 'thumb', 't');
        INSERT INTO shared_types (id, name, kind, size, definition, fields_json, origin, created_at)
            VALUES (1, 'UnkStruct_00001000', 'struct', 48, 'typedef struct {} X;', '[]',
                    'merge', 't');
        INSERT INTO provisional_types (function_id, name, kind, size, fields_json, signature,
            source, shared_type_id, created_at)
            VALUES (1, 'UnkStruct_00001000', 'struct', 48, '[]', '44:2', 'src', 1, 't');
        INSERT INTO type_uses (shared_type_id, function_id) VALUES (1, 1);
        INSERT INTO name_proposals (subject, shared_type_id, current_name, proposed_name,
            evidence_json, created_at) VALUES ('type', 1, 'UnkStruct_00001000', 'Thing', '{}', 't');
        """
    )
    assert conn.execute("SELECT status FROM shared_types").fetchone()[0] == "proposed"
    assert conn.execute("SELECT status FROM name_proposals").fetchone()[0] == "pending"
    with pytest.raises(sqlite3.IntegrityError):  # one row per (function, type name)
        conn.execute(
            "INSERT INTO provisional_types (function_id, name, kind, fields_json, signature, "
            "source, created_at) VALUES (1, 'UnkStruct_00001000', 'struct', '[]', '', '', 't')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE shared_types SET origin = 'guess'")
