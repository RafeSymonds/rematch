"""Phase 2 units: history, sets, provisional types, seal, invocation, report, prompts."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from rematch import prompts
from rematch.bench.report import append_row, benchmarks_row, run_metrics
from rematch.bench.sets import bucket, quotas, set_id, stratified_sample
from rematch.db import store
from rematch.inventory.c_parser import strip_comments
from rematch.inventory.history import (
    Presence,
    TreeReader,
    commit_before,
    decompiled_dates,
    first_parent_range,
    parse_link_order_lenient,
    resolve_object,
)
from rematch.types.provisional import TypesError, check_block, extract_block, parse_block
from rematch.worker.invoke import InvokeConfig, deny_rule, run_worker, worker_argv
from rematch.worker.sandbox import seal, verify_seal
from tests.helpers import git

# --- history -----------------------------------------------------------------------


def _asm(funcs: dict[str, int]) -> str:
    return "".join(
        f"\tthumb_func_start {n}\n{n}: ; {a:#010x}\n\tbx lr\n\tthumb_func_end {n}\n"
        for n, a in funcs.items()
    )


def _commit(repo: Path, date: str, files: dict[str, str | None]) -> None:
    for path, text in files.items():
        full = repo / path
        if text is None:
            full.unlink()
        else:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(text)
    git(repo, "add", "-A")
    env_date = f"{date}T12:00:00+00:00"
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", date],
        check=True,
        env={
            **__import__("os").environ,
            "GIT_COMMITTER_DATE": env_date,
            "GIT_AUTHOR_DATE": env_date,
        },
    )


def test_history_bisects_post_cutoff_dates(tmp_path: Path) -> None:
    repo = tmp_path / "t"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    old_lsf = "Static main\n{\n    Object asm/a.o\n}\nOverlay OLD_NAME\n{\n    Object asm/ov.o\n}\n"
    _commit(
        repo,
        "2026-05-01",
        {
            "main.lsf": old_lsf,
            "asm/a.s": _asm({"F1": 0x100, "F2": 0x200, "F3": 0x300}),
            "asm/ov.s": _asm({"G1": 0x100}),
        },
    )
    _commit(repo, "2026-06-01", {"asm/a.s": _asm({"F2": 0x200, "F3": 0x300})})  # F1 pre-cutoff
    _commit(repo, "2026-08-01", {"asm/a.s": _asm({"F3": 0x300})})  # F2
    new_lsf = "Static main\n{\n    Object asm/a.s\n}\nOverlay new_name\n{\n    Object src/ov.c\n}\n"
    _commit(
        repo,
        "2026-09-01",
        {"main.lsf": None, "link.lsf": new_lsf, "asm/ov.s": None, "src/ov.c": "int G1;\n"},
    )  # G1 (overlay renamed)
    _commit(repo, "2026-10-01", {"asm/a.s": _asm({}), "src/a.c": "int F3;\n"})  # F3
    pin = git(repo, "rev-parse", "HEAD")

    cutoff = commit_before(repo, pin, "2026-07-01")
    assert cutoff is not None and cutoff.date.startswith("2026-06-01")
    commits = first_parent_range(repo, cutoff.sha, pin)
    assert [c.date[:10] for c in commits] == ["2026-08-01", "2026-09-01", "2026-10-01"]
    targets = {1: ("main", 0x100), 2: ("main", 0x200), 3: ("main", 0x300), 4: ("overlay:0", 0x100)}
    with TreeReader(repo) as reader:
        presence = Presence(reader, ["link.lsf", "main.lsf"], "pret-macros")
        dates = decompiled_dates(presence, targets, cutoff, commits, lambda _m: None)
    assert {fid: c.date[:10] for fid, c in dates.items()} == {
        2: "2026-08-01",
        3: "2026-10-01",
        4: "2026-09-01",
    }


def test_lenient_link_order_and_resolution() -> None:
    tus = parse_link_order_lenient(
        "Static main\n{\n    Object lib/x.o (.itcm)\n    Object asm/y.o\n}\n"
    )
    assert [t.path for t in tus] == ["lib/x.s", "asm/y.s"]
    assert resolve_object("asm/y.s", {"asm/y.s"}) == "asm/y.s"
    assert resolve_object("lib/x.s", {"lib/x.c"}) is None


# --- sets ----------------------------------------------------------------------------


def test_buckets_quotas_ids() -> None:
    assert [bucket(n) for n in (0, 39, 40, 149, 150)] == ["S", "S", "M", "M", "L"]
    assert quotas({"S": 60, "M": 30, "L": 10}, 10) == {"S": 6, "M": 3, "L": 1}
    assert sum(quotas({"S": 1, "M": 1, "L": 1}, 2).values()) == 2
    assert set_id(100, 42, "2026-07-01") == "b100-s42-c20260701"


def test_stratified_sample_is_seeded_and_filters() -> None:
    sizes = {i: (10 if i < 60 else 80 if i < 90 else 200) for i in range(100)}
    bad = {0, 1, 2, 3, 60, 61}
    tried: list[int] = []

    def eligible(ids: list[int]) -> list[int]:
        tried.extend(ids)
        return [i for i in ids if i not in bad]

    a, rejected = stratified_sample(sizes, 10, 42, eligible)
    b, _ = stratified_sample(sizes, 10, 42, lambda ids: [i for i in ids if i not in bad])
    assert a == b and len(a) == 10 and not set(a) & bad
    assert sum(1 for i in a if i < 60) == 6
    assert set(rejected) <= bad
    assert len(tried) < 40  # only about n candidates are screened


# --- provisional types -----------------------------------------------------------------

BLOCK = """/* rematch:types */
typedef struct UnkStruct_02001000 {
    u8 unk_00[3];
    u16 unk_04; // comment
    u32 unk_08 : 4;
} UnkStruct_02001000;
/* rematch:end-types */
int F(UnkStruct_02001000 *p) { return p->unk_04; }
"""


def test_provisional_block_parses() -> None:
    body = extract_block(BLOCK)
    assert body is not None
    (t,) = parse_block(body)
    assert t.name == "UnkStruct_02001000" and t.kind == "struct"
    assert [(f.name, f.bitfield) for f in t.fields] == [
        ("unk_00", False),
        ("unk_04", False),
        ("unk_08", True),
    ]
    assert extract_block("int F(void) { return 0; }") is None
    assert check_block(BLOCK.replace("unk_08 : 4", "unk_08_0 : 4"), "") is None  # bitfield suffix
    fptr = BLOCK.replace("u16 unk_04; // comment", "void (*unk_04[3])(void);")
    assert check_block(fptr, "") is None
    assert check_block(BLOCK, "typedef int Other;") is None


@pytest.mark.parametrize(
    ("candidate", "message"),
    [
        ("int x;\n" + BLOCK, "must come first"),
        (BLOCK + BLOCK, "more than one"),
        ("/* rematch:types */\ntypedef struct UnkStruct_02001000 { u8 unk_00; } X;\n", "end-types"),
        (BLOCK.replace("UnkStruct_02001000", "MyStruct"), "UnkStruct_"),
        (BLOCK.replace("unk_04;", "count;"), "unk_XX"),
        (BLOCK.replace("unk_04;", "unk_04x;"), "unk_XX"),
        (BLOCK.replace("u16 unk_04; // comment", "struct { u8 unk_0; } unk_04;"), "nested"),
        (BLOCK.replace("typedef struct", "struct", 1), "only `typedef"),
    ],
)
def test_provisional_block_rejections(candidate: str, message: str) -> None:
    assert message in (check_block(candidate, "") or "")


def test_provisional_block_conflicts_with_ctx() -> None:
    reason = check_block(BLOCK, "typedef struct UnkStruct_02001000 UnkStruct_02001000;")
    assert reason is not None and "redefines" in reason
    with pytest.raises(TypesError):
        parse_block("typedef struct UnkStruct_02001000 { } UnkStruct_02001000;")


def test_strip_comments_keeps_strings() -> None:
    src = 'int a; // x\nconst char *s = "// not a comment"; /* gone */ int b;'
    assert strip_comments(src) == 'int a; \nconst char *s = "// not a comment";   int b;'


# --- seal ----------------------------------------------------------------------------


def test_seal_detects_changes(tmp_path: Path) -> None:
    for name in ("ctx.h", "target.s", "candidate.c", "NOTES.md", "check"):
        (tmp_path / name).write_text(name)
    (tmp_path / ".history").mkdir()
    manifest = seal(tmp_path)
    assert set(manifest) == {"ctx.h", "target.s", "check"}
    assert (tmp_path / "ctx.h").stat().st_mode & 0o222 == 0
    (tmp_path / "candidate.c").write_text("edited")  # writable files are not sealed
    (tmp_path / ".history" / "001.json").write_text("{}")
    assert verify_seal(tmp_path, manifest) == []
    (tmp_path / "ctx.h").chmod(0o644)
    (tmp_path / "ctx.h").write_text("cheat")
    (tmp_path / "extra.c").write_text("x")
    (tmp_path / "target.s").unlink()
    assert verify_seal(tmp_path, manifest) == [
        "modified ctx.h",
        "missing target.s",
        "added extra.c",
    ]


# --- invocation ----------------------------------------------------------------------


def _cfg(
    tmp_path: Path, claude: Path, timeout: float = 30, auth: str = "subscription"
) -> InvokeConfig:
    return InvokeConfig(
        claude_bin=str(claude),
        model="m",
        effort="high",
        auth_mode=auth,
        max_turns=7,
        timeout_sec=timeout,
        max_budget_usd=1.5,
        system_prompt_file=tmp_path / "sys.md",
        deny_read_roots=(tmp_path / "secret",),
        api_key="sk-test",
    )


def test_worker_argv_isolation_flags(tmp_path: Path) -> None:
    argv = worker_argv(_cfg(tmp_path, Path("claude")), "PROMPT")
    joined = " ".join(argv)
    for flag in (
        "--permission-mode dontAsk",
        "--permission-prompts none",
        "--setting-sources project,local",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--max-turns 7",
        "--output-format stream-json",
        "Bash(./check)",
        deny_rule(tmp_path / "secret"),
    ):
        assert flag in joined
    assert "--bare" not in argv and "Bash" not in argv[argv.index("--allowedTools") + 1 :][:1]
    assert deny_rule(Path("/a/b")) == "Read(//a/b/**)"
    assert "--no-session-persistence" not in argv  # sessions persist so rate limits can resume
    resumed = worker_argv(_cfg(tmp_path, Path("claude")), "P", resume="sess-1")
    assert resumed[resumed.index("--resume") + 1] == "sess-1"
    api = worker_argv(_cfg(tmp_path, Path("claude"), auth="api"), "P")
    assert "--bare" in api and "--max-budget-usd" in api


def _fake_claude(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake_claude"
    path.write_text(f"#!{sys.executable}\nimport json, signal, sys, time\n{body}\n")
    path.chmod(0o755)
    return path


def test_run_worker_streams_and_parses_result(tmp_path: Path) -> None:
    claude = _fake_claude(
        tmp_path,
        (
            "print(json.dumps({'type': 'system', 'subtype': 'init'}))\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'num_turns': 3,"
            " 'total_cost_usd': 0.25, 'usage': {'input_tokens': 10, 'output_tokens': 5,"
            " 'cache_read_input_tokens': 7}}))"
        ),
    )
    stream = tmp_path / "logs" / "1.jsonl"
    r = run_worker(_cfg(tmp_path, claude), tmp_path, "PROMPT", stream)
    assert r.exit_code == 0 and not r.timed_out and r.events == 2
    assert (
        r.num_turns,
        r.cost_usd,
        r.usage("input_tokens"),
        r.usage("cache_read_input_tokens"),
    ) == (3, 0.25, 10, 7)
    assert len(stream.read_text().splitlines()) == 2


def test_run_worker_timeout_sends_sigint_first(tmp_path: Path) -> None:
    claude = _fake_claude(
        tmp_path,
        (
            "def stop(*_):\n"
            "    print(json.dumps({'type': 'result', 'subtype': 'interrupted', 'num_turns': 1}))\n"
            "    sys.stdout.flush(); sys.exit(130)\n"
            "signal.signal(signal.SIGINT, stop)\n"
            "time.sleep(60)"
        ),
    )
    r = run_worker(_cfg(tmp_path, claude, timeout=1), tmp_path, "P", tmp_path / "s.jsonl")
    assert r.timed_out and r.exit_code == 130 and r.subtype == "interrupted"


def test_subscription_mode_drops_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    claude = _fake_claude(
        tmp_path,
        (
            "import os\nprint(json.dumps({'type': 'result', 'subtype': 'success',"
            " 'key': os.environ.get('ANTHROPIC_API_KEY')}))"
        ),
    )
    r = run_worker(_cfg(tmp_path, claude), tmp_path, "P", tmp_path / "s.jsonl")
    assert r.result is not None and r.result["key"] is None


# --- report and prompts --------------------------------------------------------------


def test_run_metrics_and_row(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite")
    conn.execute(
        "INSERT INTO runs (id, kind, bench_set, config_json, started_at) VALUES "
        "(1, 'bench', 'b2-s1-c20260701', ?, '2026-11-26T00:00:00+00:00')",
        (json.dumps({"model": "m", "effort": "e", "prompt_version": 1, "context_version": 1}),),
    )
    for fid, n in ((1, 10), (2, 100)):
        conn.execute(
            "INSERT INTO functions (id, name, address, module, tu_path, source_kind, mode,"
            " n_insns, updated_at) VALUES (?, ?, ?, 'main', 't.c', 'c_human', 'thumb', ?, 't')",
            (fid, f"f{fid}", fid, n),
        )
    conn.execute(
        "INSERT INTO attempts (run_id, function_id, task_kind, outcome, num_turns,"
        " input_tokens, output_tokens, cost_usd, started_at, ended_at, best_candidate_path)"
        " VALUES (1, 1, 'function', 'match', 4, 100, 50, 0.5, '2026-11-26T00:00:00+00:00',"
        " '2026-11-26T00:01:00+00:00', '/x/.history/003.c')"
    )
    conn.execute(
        "INSERT INTO attempts (run_id, function_id, task_kind, outcome, num_turns,"
        " cost_usd) VALUES (1, 2, 'function', 'stagnation', 9, 1.5)"
    )
    conn.commit()
    m = run_metrics(conn, 1)
    b = m["buckets"]
    assert isinstance(b, dict)
    assert b["all"]["matches"] == 1 and b["all"]["match_rate"] == 50.0
    assert b["S"]["match_rate"] == 100.0 and b["M"]["match_rate"] == 0.0
    assert b["all"]["cost_per_match_usd"] == 2.0 and b["all"]["median_checks_to_match"] == 3
    assert b["all"]["outcomes"] == {"match": 1, "stagnation": 1}
    row = benchmarks_row(m, "demo", "note")
    assert row.startswith(
        "| 2026-11-26 | demo | 1 | b2-s1-c20260701 | 1 | 1 | m:e | 50.0 (100/0/0)"
    )
    path = append_row(tmp_path, row)
    append_row(tmp_path, row)
    assert path.read_text().count("| demo |") == 2 and path.read_text().count("| date |") == 1
    assert isinstance(conn, sqlite3.Connection)


def test_prompts_render() -> None:
    assert prompts.prompt_version() >= 1
    text = prompts.worker_system("A test target.")
    assert "A test target." in text and "{{" not in text
    task = prompts.render(
        "task.md.j2",
        symbol="sub_1",
        mode="thumb",
        n_insns=5,
        module="main",
        examples=["00_a"],
        draft=None,
        previous_best=None,
        previous_best_score=None,
        max_checks=40,
        version_cond=True,
        target_s="\tbx lr",
        declarations="void sub_1(void);",
        ctx_kb=1000,
        learnings="(none)",
    )
    assert "\tbx lr" in task and "void sub_1(void);" in task and "1000 KB" in task
    assert "sub_1" in task and "every target version" in task and "draft.c" not in task


# --- rate limits and resume ------------------------------------------------------------


def test_rate_limit_wait_classification(tmp_path: Path) -> None:
    from rematch.worker.invoke import InvokeResult, rate_limit_wait

    def res(result: dict[str, object] | None, stderr: str = "") -> InvokeResult:
        return InvokeResult(exit_code=1, seconds=1.0, result=result, stderr_tail=stderr)

    assert rate_limit_wait(res({"is_error": False, "subtype": "success"}), 0, 0) is None
    assert (
        rate_limit_wait(res({"is_error": True, "result": "API Error: 429 rate limit"}), 0, 0) == 60
    )
    assert rate_limit_wait(res({"is_error": True, "result": "Overloaded"}), 0, 3) == 480
    assert rate_limit_wait(res({"is_error": True, "result": "Overloaded"}), 0, 9) == 900
    usage = res({"is_error": True, "result": "Claude AI usage limit reached|1764200600"})
    assert rate_limit_wait(usage, 1764200000, 0) == 600
    assert rate_limit_wait(res(None, "Error: 529 overloaded"), 0, 1) == 120
    assert rate_limit_wait(res({"is_error": True, "result": "compile crashed"}), 0, 0) is None


def test_rate_limited_attempt_resumes_session(tmp_path: Path) -> None:
    from rematch.worker.attempt import AttemptSpec, _run_segments
    from rematch.worker.sandbox import FunctionRecord, SandboxOptions

    calls = tmp_path / "calls.log"
    claude = _fake_claude(
        tmp_path,
        (
            f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "resumed = '--resume' in sys.argv\n"
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'sess-42'}))\n"
            "if resumed:\n"
            "    print(json.dumps({'type': 'result', 'subtype': 'success', 'num_turns': 4,"
            " 'session_id': 'sess-42', 'total_cost_usd': 0.1}))\n"
            "else:\n"
            "    print(json.dumps({'type': 'result', 'subtype': 'error', 'is_error': True,"
            " 'num_turns': 3, 'result': 'API Error: 429 rate limit', 'total_cost_usd': 0.05}))"
        ),
    )
    (tmp_path / "TASK.md").write_text("# Task: match F (thumb, 1 instructions, module m)\n")

    class Gate:
        def __init__(self) -> None:
            self.paused: list[float] = []

        def pause(self, seconds: float) -> None:
            self.paused.append(seconds)

        def wait(self) -> None:
            return None

    gate = Gate()
    fn = FunctionRecord(1, "F", 0, "m", "t.c", "c_human", "thumb", False)
    spec = AttemptSpec(
        run_id=1,
        kind="bench",
        fn=fn,
        options=SandboxOptions(),
        model="m",
        effort="e",
        system_prompt=tmp_path / "sys.md",
        gate=gate,
    )
    result, totals = _run_segments(
        _cfg(tmp_path, claude), tmp_path, tmp_path / "s.jsonl", spec, lambda _m: None
    )
    assert result.subtype == "success" and gate.paused == [60.0]
    assert (totals.segments, totals.turns) == (2, 7) and totals.cost_usd == pytest.approx(0.15)
    argvs = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(argvs) == 2 and "--resume" not in argvs[0]
    assert argvs[1][argvs[1].index("--resume") + 1] == "sess-42"
    assert (tmp_path / "s.r1.jsonl").is_file()
