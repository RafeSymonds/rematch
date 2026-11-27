from __future__ import annotations

from pathlib import Path

from rematch.bench.sets import live_sample
from rematch.db import store


def test_live_sample_is_stratified_and_deterministic(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite")
    rows = [(i, 10) for i in range(60)] + [(100 + i, 80) for i in range(30)]
    rows += [(200 + i, 400) for i in range(10)] + [(300, 10)]
    for fid, n in rows:
        kind, status = ("c_human", "untried") if fid == 300 else ("asm", "untried")
        conn.execute(
            "INSERT INTO functions (id, name, address, module, tu_path, source_kind, mode,"
            " n_insns, status, updated_at)"
            " VALUES (?, ?, ?, 'main', 'asm/a.s', ?, 'thumb', ?, ?, 't')",
            (fid, f"f{fid}", fid * 4, kind, n, status),
        )
    conn.execute("UPDATE functions SET status = 'matched' WHERE id = 0")
    conn.commit()
    picked = live_sample(conn, 10, seed=1)
    assert picked == live_sample(conn, 10, seed=1)
    assert len(picked) == 10 and 0 not in picked and 300 not in picked
    sizes = [n for fid, n in rows if fid in picked]
    assert (sizes.count(10), sizes.count(80), sizes.count(400)) == (6, 3, 1)
