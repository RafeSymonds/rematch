"""Shared pieces of the fake `claude` agents: stream-json events and the invocation log."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def emit(event: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def init_event(agent: str) -> None:
    emit(
        {
            "type": "system",
            "subtype": "init",
            "model": f"fake-{agent}",
            "tools": [],
            "cwd": os.getcwd(),
            "fake": True,
        }
    )


def result_event(text: str, turns: int) -> None:
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": turns,
            "result": text,
            "total_cost_usd": 0.0,
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0},
        }
    )


def log_invocation(agent: str, record: dict[str, object]) -> None:
    path = os.environ.get("REMATCH_FAKE_LOG")
    if not path:
        return
    entry = {
        "agent": agent,
        "cwd": os.getcwd(),
        "argv": sys.argv[1:4],
        "time": time.time(),
        **record,
    }
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
