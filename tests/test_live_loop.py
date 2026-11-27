from __future__ import annotations

from pathlib import Path

from rematch.db import store
from rematch.live.loop import acquire, pick_order, recover_leases, settle
from rematch.worker.outcome import Classified


def _db(tmp_path: Path):  # type: ignore[no-untyped-def]
    conn = store.connect(tmp_path / "db.sqlite")
    rows = [
        (1, "asm/big.s", 0x10, "untried"),
        (2, "asm/big.s", 0x20, "untried"),
        (3, "asm/big.s", 0x30, "untried"),
        (4, "asm/small.s", 0x40, "matched"),
        (5, "asm/small.s", 0x48, "untried"),
        (6, "asm/small.s", 0x44, "failed"),
    ]
    for fid, tu, addr, status in rows:
        conn.execute(
            "INSERT INTO functions (id, name, address, module, tu_path, source_kind, mode,"
            " n_insns, status, attempts, updated_at)"
            " VALUES (?, ?, ?, 'main', ?, 'asm', 'thumb', 10, ?, ?, '2000-01-01T00:00:00+00:00')",
            (fid, f"f{fid}", addr, tu, status, 1 if status == "failed" else 0),
        )
    conn.commit()
    return conn


def test_pick_order_prefers_nearly_done_tus_in_address_order(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert pick_order(conn, n_rungs=4, cooldown=0) == [(6, 0), (5, 0), (1, 0), (2, 0), (3, 0)]
    conn.execute("UPDATE functions SET escalation = 4 WHERE id = 6")  # ladder exhausted
    assert [f for f, _ in pick_order(conn, 4, 0)] == [5, 1, 2, 3]


def test_lease_settle_and_recover(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert acquire(conn, 1, "me", ttl=60) and not acquire(conn, 1, "other", ttl=60)
    settle(conn, 1, Classified("nonmatch"), n_rungs=2)
    row = conn.execute("SELECT status, escalation, lease_owner FROM functions WHERE id = 1")
    assert row.fetchone() == ("failed", 1, None)
    assert acquire(conn, 1, "me", ttl=60)
    settle(conn, 1, Classified("stagnation"), n_rungs=2)
    assert conn.execute("SELECT status FROM functions WHERE id = 1").fetchone()[0] == "stuck"
    # a crashed owner's lease reverts once it expires
    assert acquire(conn, 2, "crashed", ttl=-1)
    assert recover_leases(conn) == 1
    assert conn.execute("SELECT status FROM functions WHERE id = 2").fetchone()[0] == "untried"
    # a harness error before the attempt recorded anything also gives the function back
    assert acquire(conn, 3, "me", ttl=60)
    settle(conn, 3, None, n_rungs=2)
    assert conn.execute("SELECT status FROM functions WHERE id = 3").fetchone()[0] == "untried"


def test_dead_local_owner_lease_is_recovered(tmp_path: Path) -> None:
    import socket

    conn = _db(tmp_path)
    assert acquire(conn, 1, f"{socket.gethostname()}:999999999", ttl=3600)  # no such pid
    assert acquire(conn, 2, "elsewhere:1", ttl=3600)  # another host: left alone
    assert recover_leases(conn) == 1
    rows = dict(conn.execute("SELECT id, status FROM functions WHERE id IN (1, 2)"))
    assert rows == {1: "untried", 2: "leased"}
