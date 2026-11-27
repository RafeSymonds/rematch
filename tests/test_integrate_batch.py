"""Batched integration: one build per batch, culprit by name or by halves (SPEC §10)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rematch.db import store
from rematch.integrate import run as R
from rematch.integrate.linkspec import WRITERS
from tests.helpers import git

LINK = "link.txt"


def writer(text: str, old: list[str], new: list[str]) -> str:
    lines = [ln for ln in text.splitlines() if ln not in old]
    return "\n".join([*lines, *new]) + "\n"


def prepared(tu: str) -> R.PreparedTU:
    path = f"src/{tu}.c"
    plan = R.TUPlan(f"asm/{tu}.s", [f"{tu}_f"], [f"{tu}_f"], [], [f"asm/{tu}.s"])
    return R.PreparedTU(plan, {path: f"int {tu};\n"}, [path], f"hash-{tu}", f"{tu} to C")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    live = tmp_path / "live"
    (live / "src").mkdir(parents=True)
    (live / "asm").mkdir()
    git(live, "init", "-q", "-b", "rematch/live")
    for tu in "abcd":
        (live / "asm" / f"{tu}.s").write_text(f"; {tu}\n")
    (live / LINK).write_text("".join(f"asm/{tu}.s\n" for tu in "abcd"))
    git(live, "add", "-A")
    git(live, "commit", "-q", "-m", "init")
    s = SimpleNamespace(settings=SimpleNamespace(rematch_var=tmp_path, live_worktree=live))
    conn = store.connect(tmp_path / "db.sqlite")
    builds: list[list[str]] = []
    bad: set[str] = set()
    name_in_error: list[bool] = [False]

    def fake_build(_s: Any, worktree: Path, _log: Any) -> list[str]:
        present = sorted(p.stem for p in (worktree / "src").glob("*.c"))
        builds.append(present)
        broken = [t for t in present if t in bad]
        if not broken:
            return []
        where = f" in src_{broken[0]}.c.o" if name_in_error[0] else ""
        return [f"v1: link failed{where}"]

    monkeypatch.setattr(R, "_build_all", fake_build)
    return SimpleNamespace(
        s=s, conn=conn, live=live, builds=builds, bad=bad, name_in_error=name_in_error
    )


def land(env: Any, tus: str) -> list[R.Outcome]:
    out: list[R.Outcome] = []
    R._land(env.s, env.conn, env.live, LINK, writer, [prepared(t) for t in tus], out, print)
    return out


def commits(live: Path) -> list[str]:
    return git(live, "log", "--format=%s").splitlines()


def test_whole_batch_lands_in_one_build_and_one_commit(env: Any) -> None:
    out = land(env, "abc")
    assert [o.status for o in out] == ["committed"] * 3
    assert len(env.builds) == 1
    assert commits(env.live)[0] == "rematch: integrate 3 TUs (3 functions)"
    shas = {r[0] for r in env.conn.execute("SELECT commit_sha FROM integrations")}
    assert len(shas) == 1
    assert not git(env.live, "status", "--porcelain")
    assert (env.live / LINK).read_text().splitlines() == ["asm/d.s", *[f"src/{t}.c" for t in "abc"]]


def test_bisect_commits_each_passing_half_before_the_next(env: Any) -> None:
    env.bad.add("b")
    out = land(env, "abcd")
    status = {o.tu: o.status for o in out}
    assert status == {
        "asm/a.s": "committed",
        "asm/b.s": "failed",
        "asm/c.s": "committed",
        "asm/d.s": "committed",
    }
    # abcd fails, ab fails, a passes (committed), b fails alone, cd passes on top of a
    assert env.builds == [["a", "b", "c", "d"], ["a", "b"], ["a"], ["a", "b"], ["a", "c", "d"]]
    assert commits(env.live)[:2] == ["rematch: integrate 2 TUs (2 functions)", "rematch: a to C"]
    assert not (env.live / "src" / "b.c").exists()
    row = env.conn.execute("SELECT plan_hash FROM integrations WHERE status = 'failed'").fetchone()
    assert row[0] == "hash-b"


def test_culprit_named_in_the_build_output_is_dropped_without_bisecting(env: Any) -> None:
    env.bad.add("c")
    env.name_in_error[0] = True
    out = land(env, "abcd")
    assert {o.tu for o in out if o.status == "failed"} == {"asm/c.s"}
    assert env.builds == [["a", "b", "c", "d"], ["a", "b", "d"]]


def test_exception_restores_the_worktree(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any) -> list[str]:
        raise RuntimeError("container died")

    monkeypatch.setattr(R, "_build_all", boom)
    with pytest.raises(RuntimeError):
        land(env, "ab")
    assert not git(env.live, "status", "--porcelain")
    assert not (env.s.settings.rematch_var / "integrate.journal").exists()


def test_journal_replay_cleans_a_killed_batch(env: Any) -> None:
    R._apply(env.s, env.live, LINK, writer, [prepared("a")])  # written, never built
    assert git(env.live, "status", "--porcelain")
    R.recover_journal(env.s, print)  # type: ignore[arg-type]
    assert not git(env.live, "status", "--porcelain")
    assert not (env.s.settings.rematch_var / "integrate.journal").exists()


def test_failed_before_sees_failures_behind_later_rows(env: Any) -> None:
    plan = prepared("a").plan
    R._record(env.conn, plan, "failed", None, "data", "h-data")
    R._record(env.conn, plan, "failed", None, "types", "h-types")  # the other step, later
    assert R.failed_before(env.conn, "asm/a.s", "h-data")
    assert R.failed_before(env.conn, "asm/a.s", "h-types")
    R._record(env.conn, plan, "committed", "abc", "y", "h2")
    assert not R.failed_before(env.conn, "asm/a.s", "h2")
    assert not R.failed_before(env.conn, "asm/b.s", "h-data")


def test_culprit_needs_a_unique_name() -> None:
    batch = [prepared("a"), prepared("ab")]
    assert R.culprit(["undefined in src_ab.c.o"], batch) is batch[1]
    assert R.culprit(["hash mismatch"], batch) is None
    assert json.dumps(sorted(batch[0].files)) == '["src/a.c"]'


def test_a_step_that_rewrites_one_piece_keeps_the_others(env: Any) -> None:
    # types step: the TU keeps its pieces; only the C file changes
    plan = R.TUPlan("asm/a.s", ["a_f"], [], [], ["asm/a.s"])
    step = R.PreparedTU(plan, {"src/a.c": "int a2;\n"}, ["asm/a.s"], "h", "a types merged")
    out: list[R.Outcome] = []
    R._land(env.s, env.conn, env.live, LINK, writer, [step], out, print)
    assert out[0].status == "committed"
    assert (env.live / "asm" / "a.s").is_file()


def test_steps_share_one_pool_and_finish_falls_through_to_data(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    def fn_step(_s: Any, _c: Any, tu: str, **_k: Any) -> Any:
        calls.append(("functions", tu))
        return prepared(tu[4:5])

    def types_step(_s: Any, _c: Any, tu: str, **_k: Any) -> Any:
        calls.append(("types", tu))
        if tu == "asm/b.s":
            return prepared("b")  # types land; data waits for the next cycle
        return R.Outcome(tu, "skipped", "types already merged")

    def data_step(_s: Any, _c: Any, tu: str, **_k: Any) -> Any:
        calls.append(("data", tu))
        return prepared(tu[4:5])

    monkeypatch.setattr(R, "prepare_tu", fn_step)
    monkeypatch.setattr(R, "prepare_types_tu", types_step)
    monkeypatch.setattr(R, "prepare_data_tu", data_step)
    monkeypatch.setattr(R, "live_worktree", lambda _s: env.live)
    monkeypatch.setitem(WRITERS, "test", writer)
    monkeypatch.setitem(R._batch, "size", R.BATCH_START)
    link = SimpleNamespace(format="test", file=LINK)
    env.s.definition = SimpleNamespace(
        asm=SimpleNamespace(format="pret-macros"), link_order=link, require=lambda v, _n: v
    )
    env.s.settings.integrate_workers = 2
    env.s.db_path = env.s.settings.rematch_var / "db.sqlite"
    out = R.integrate(
        env.s,
        env.conn,
        [],
        dry_run=False,
        log=lambda _m: None,
        steps=[("functions", ["asm/a.s"]), ("finish", ["asm/b.s", "asm/c.s"])],
    )
    assert sorted(calls) == [
        ("data", "asm/c.s"),
        ("functions", "asm/a.s"),
        ("types", "asm/b.s"),
        ("types", "asm/c.s"),
    ]
    assert sorted(o.tu for o in out if o.status == "committed") == ["asm/a.s", "asm/b.s", "asm/c.s"]
    assert len(env.builds) == 1  # one batch, one build


def test_landing_applies_prototype_overrides_to_live_headers(env: Any) -> None:
    (env.live / "include").mkdir()
    (env.live / "include" / "x.h").write_text("// x\nvoid f(void);\nint g(void);\n")
    git(env.live, "add", "-A")
    git(env.live, "commit", "-q", "-m", "header")
    env.s.definition = SimpleNamespace(build_info=SimpleNamespace(header_dirs=["include"]))
    p = prepared("a")
    p.overrides = {"f": "BOOL f(u16 a);"}
    out: list[R.Outcome] = []
    assert R._land(env.s, env.conn, env.live, LINK, writer, [p], out, print)  # type: ignore[arg-type]
    assert (env.live / "include" / "x.h").read_text() == "// x\nBOOL f(u16 a);\nint g(void);\n"
    assert "include/x.h" in git(env.live, "show", "--name-only", "--format=", "HEAD")
