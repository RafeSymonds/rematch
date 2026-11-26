"""Worker prompts (SPEC Appendix A/B): rendering and versions."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import jinja2

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
_VERSION = re.compile(r"^prompt_version:\s*(\d+)\s*$", re.MULTILINE)

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)),
    undefined=jinja2.StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
)


def prompt_version() -> int:
    m = _VERSION.search((PROMPTS_DIR / "worker_system.md").read_text(encoding="utf-8"))
    if m is None:
        raise ValueError("worker_system.md has no `prompt_version: N` line")
    return int(m.group(1))


def render(name: str, **values: Any) -> str:
    return _env.get_template(name).render(**values)


def worker_system(target_intro: str) -> str:
    return render("worker_system.md", target_intro=target_intro)
