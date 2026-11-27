from __future__ import annotations

from pathlib import Path

from rematch.db import store
from rematch.live.loop import Rung, acquire, function_ladder, pick_order, recover_leases, settle
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
    four = [Rung("m", "e")] * 4
    assert pick_order(conn, four, cooldown=0) == [(6, 0), (5, 0), (1, 0), (2, 0), (3, 0)]
    conn.execute("UPDATE functions SET escalation = 4 WHERE id = 6")  # ladder exhausted
    assert [f for f, _ in pick_order(conn, four, 0)] == [5, 1, 2, 3]


def test_lease_settle_and_recover(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert acquire(conn, 1, "me", ttl=60) and not acquire(conn, 1, "other", ttl=60)
    settle(conn, 1, Classified("nonmatch"), [Rung("m", "e")] * 2)
    row = conn.execute("SELECT status, escalation, lease_owner FROM functions WHERE id = 1")
    assert row.fetchone() == ("failed", 1, None)
    assert acquire(conn, 1, "me", ttl=60)
    settle(conn, 1, Classified("stagnation"), [Rung("m", "e")] * 2)
    assert conn.execute("SELECT status FROM functions WHERE id = 1").fetchone()[0] == "stuck"
    # a crashed owner's lease reverts once it expires
    assert acquire(conn, 2, "crashed", ttl=-1)
    assert recover_leases(conn) == 1
    assert conn.execute("SELECT status FROM functions WHERE id = 2").fetchone()[0] == "untried"
    # a harness error before the attempt recorded anything also gives the function back
    assert acquire(conn, 3, "me", ttl=60)
    settle(conn, 3, None, [Rung("m", "e")] * 2)
    assert conn.execute("SELECT status FROM functions WHERE id = 3").fetchone()[0] == "untried"


def test_dead_local_owner_lease_is_recovered(tmp_path: Path) -> None:
    import socket

    conn = _db(tmp_path)
    assert acquire(conn, 1, f"{socket.gethostname()}:999999999", ttl=3600)  # no such pid
    assert acquire(conn, 2, "elsewhere:1", ttl=3600)  # another host: left alone
    assert recover_leases(conn) == 1
    rows = dict(conn.execute("SELECT id, status FROM functions WHERE id IN (1, 2)"))
    assert rows == {1: "untried", 2: "leased"}


def test_size_gated_rungs_apply_only_to_small_functions(tmp_path: Path) -> None:
    rungs = [Rung("haiku", "medium", 50), Rung("sonnet", "medium"), Rung("opus", "high")]
    assert [r.model for r in function_ladder(rungs, 20)] == ["haiku", "sonnet", "opus"]
    assert [r.model for r in function_ladder(rungs, 80)] == ["sonnet", "opus"]
    conn = _db(tmp_path)  # every function has 10 instructions
    conn.execute("UPDATE functions SET n_insns = 80 WHERE id = 1")
    assert acquire(conn, 1, "me", ttl=60)
    settle(conn, 1, Classified("nonmatch"), rungs)
    assert acquire(conn, 1, "me", ttl=60)
    settle(conn, 1, Classified("nonmatch"), rungs)  # the big function's ladder has two rungs
    assert conn.execute("SELECT status FROM functions WHERE id = 1").fetchone()[0] == "stuck"
    assert acquire(conn, 2, "me", ttl=60)
    settle(conn, 2, Classified("nonmatch"), rungs)
    settle(conn, 2, Classified("nonmatch"), rungs)  # small: three rungs, one left
    assert conn.execute("SELECT status, escalation FROM functions WHERE id = 2").fetchone() == (
        "failed",
        2,
    )


def test_ladder_parses_size_limits() -> None:
    from types import SimpleNamespace
    from typing import Any

    from rematch.live.loop import ladder

    s: Any = SimpleNamespace(
        settings=SimpleNamespace(
            escalation_ladder="haiku:medium<50, sonnet:medium,opus:high",
            worker_model="x",
            worker_effort="y",
        )
    )
    assert ladder(s) == [
        Rung("haiku", "medium", 50),
        Rung("sonnet", "medium", None),
        Rung("opus", "high", None),
    ]
