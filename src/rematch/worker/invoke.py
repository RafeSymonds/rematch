"""Run one `claude -p` worker in a sandbox (SPEC §7.3).

Isolation (verified by probes, docs/verify-log.md):
- only Read/Edit/Write/Bash exist; edits are allowed on candidate.c, NOTES.md and SUMMARY.json
  only, and Bash only for the exact command `./check`; `dontAsk` + `--permission-prompts none`
  deny everything else without prompting
- reads are allowed anywhere by default, so reads of the target repo, the vanilla worktree,
  rematch itself (including the attempt's control directory under REMATCH_VAR, which names the
  human target in bench mode) and answer-bearing sandboxes are denied explicitly
- `--setting-sources project,local` drops user settings (hooks, plugins, output style)
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

SIGTERM_GRACE_SEC = 30
SIGKILL_GRACE_SEC = 10
ALLOWED_TOOLS = (
    "Read",
    "Edit(candidate.c)",
    "Write(candidate.c)",
    "Edit(NOTES.md)",
    "Write(NOTES.md)",
    "Edit(SUMMARY.json)",  # Edit(...) rules govern the Write tool too; Write(...) alone was denied
    "Write(SUMMARY.json)",
    "Bash(./check)",
)


@dataclass(frozen=True)
class InvokeConfig:
    claude_bin: str
    model: str
    effort: str
    auth_mode: str  # subscription | api
    max_turns: int
    timeout_sec: float
    max_budget_usd: float
    system_prompt_file: Path
    deny_read_roots: tuple[Path, ...]
    api_key: str = ""


@dataclass
class InvokeResult:
    exit_code: int | None
    seconds: float
    timed_out: bool = False
    result: dict[str, object] | None = None
    events: int = 0
    stream_path: Path | None = None
    stderr_tail: str = ""
    session_id: str | None = None

    @property
    def subtype(self) -> str | None:
        return None if self.result is None else str(self.result.get("subtype"))

    def usage(self, key: str) -> int:
        usage = (self.result or {}).get("usage")
        return int(usage.get(key, 0) or 0) if isinstance(usage, dict) else 0

    @property
    def num_turns(self) -> int | None:
        value = (self.result or {}).get("num_turns")
        return int(value) if isinstance(value, int) else None

    @property
    def cost_usd(self) -> float | None:
        value = (self.result or {}).get("total_cost_usd")
        return float(value) if isinstance(value, (int, float)) else None


def deny_rule(path: Path) -> str:
    """`Read(//abs/path/**)`: the `//` prefix marks an absolute path in permission rules."""
    return f"Read(//{str(path.resolve()).lstrip('/')}/**)"


def worker_argv(cfg: InvokeConfig, prompt: str, resume: str | None = None) -> list[str]:
    """Sessions are persisted (no --no-session-persistence) so a rate-limited attempt can be
    resumed with its context (`--resume`)."""
    argv = [
        cfg.claude_bin,
        "-p",
        prompt,
        *(["--resume", resume] if resume else []),
        "--model",
        cfg.model,
        "--effort",
        cfg.effort,
        "--append-system-prompt-file",
        str(cfg.system_prompt_file),
        "--tools",
        "Read,Edit,Write,Bash",
        "--allowedTools",
        *ALLOWED_TOOLS,
        "--disallowedTools",
        *(deny_rule(p) for p in cfg.deny_read_roots),
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--setting-sources",
        "project,local",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--max-turns",
        str(cfg.max_turns),
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if cfg.auth_mode == "api":
        argv += ["--bare", "--max-budget-usd", f"{cfg.max_budget_usd:g}"]
    return argv


def worker_env(cfg: InvokeConfig) -> dict[str, str]:
    env = dict(os.environ)
    if cfg.auth_mode == "api":
        env["ANTHROPIC_API_KEY"] = cfg.api_key
    else:
        env.pop("ANTHROPIC_API_KEY", None)  # never bill an API key by accident in subscription mode
    return env


@dataclass
class _Watch:
    deadline: float
    stage: int = 0  # 0 running, 1 SIGINT sent, 2 SIGTERM sent, 3 SIGKILL sent
    next_at: float = 0.0
    lines: list[str] = field(default_factory=list)


def run_worker(
    cfg: InvokeConfig, sandbox: Path, prompt: str, stream_path: Path, resume: str | None = None
) -> InvokeResult:
    """Run the worker to completion or timeout; stream events to `stream_path`."""
    stream_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    result: dict[str, object] | None = None
    session: str | None = None
    events = 0
    stderr_path = stream_path.with_suffix(".stderr")
    with stream_path.open("w", encoding="utf-8") as out, stderr_path.open("w") as err:
        proc = subprocess.Popen(
            worker_argv(cfg, prompt, resume),
            cwd=sandbox,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err,
            env=worker_env(cfg),
            text=True,
            start_new_session=True,
        )
        watch = _Watch(deadline=start + cfg.timeout_sec)

        def watchdog() -> None:
            while proc.poll() is None:
                now = time.monotonic()
                if watch.stage == 0 and now >= watch.deadline:
                    proc.send_signal(signal.SIGINT)  # ends the turn cleanly (trap 12)
                    watch.stage, watch.next_at = 1, now + SIGTERM_GRACE_SEC
                elif watch.stage == 1 and now >= watch.next_at:
                    proc.terminate()
                    watch.stage, watch.next_at = 2, now + SIGKILL_GRACE_SEC
                elif watch.stage == 2 and now >= watch.next_at:
                    proc.kill()
                    watch.stage = 3
                time.sleep(0.5)

        thread = threading.Thread(target=watchdog, daemon=True)
        thread.start()
        assert proc.stdout is not None
        for line in proc.stdout:
            out.write(line)
            events += 1
            if session is None and '"session_id"' in line:
                try:
                    sid = json.loads(line).get("session_id")
                except json.JSONDecodeError:
                    sid = None
                session = sid if isinstance(sid, str) else None
            if '"type":"result"' in line.replace(" ", ""):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "result":
                    result = event
        code = proc.wait()
        thread.join(timeout=5)
    tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    return InvokeResult(
        exit_code=code,
        seconds=round(time.monotonic() - start, 1),
        timed_out=watch.stage > 0,
        result=result,
        events=events,
        stream_path=stream_path,
        stderr_tail=tail,
        session_id=session,
    )


_LIMIT = re.compile(r"usage limit|rate.?limit|\b429\b|overloaded|\b529\b", re.IGNORECASE)
_RESET = re.compile(r"limit reached\|(\d{9,11})")


def rate_limit_wait(r: InvokeResult, now: float, attempt: int) -> float | None:
    """Seconds to wait if the run was cut short by a rate or usage limit, else None.

    Usage limits that state a reset time ("...limit reached|<epoch>") wait until then (at most
    6 h). Other limits back off exponentially: 60 s, 120 s, ... capped at 15 min.
    """
    text = ""
    if r.result is not None and r.result.get("is_error"):
        text = str(r.result.get("result", "")) + " " + str(r.result.get("subtype", ""))
    elif r.result is None:
        text = r.stderr_tail
    if not text or not _LIMIT.search(text):
        return None
    reset = _RESET.search(text)
    if reset:
        return float(min(max(int(reset.group(1)) - now, 60), 6 * 3600))
    return float(min(60 * 2**attempt, 900))
