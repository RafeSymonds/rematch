"""Builders for throwaway target repos used by doctor tests."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

ROM_BYTES = b"fake rom contents\n"
ROM_SHA1 = hashlib.sha1(ROM_BYTES).hexdigest()
FORBIDDEN = "example.invalid/upstream/"

PRE_PUSH = """#!/bin/sh
case "$2" in *example.invalid/upstream/*) echo "blocked" >&2; exit 1;; esac
exit 0
"""

TARGET_TOML = f"""
name = "demo"
default_versions = ["alpha", "beta"]
forbidden_push_urls = ["{FORBIDDEN}"]
required_paths = ["build.sh"]

[versions.alpha]
build = ["sh", "build.sh", "alpha"]
rom = "out/alpha.bin"
sha1 = "{ROM_SHA1}"

[versions.beta]
build = ["sh", "build.sh", "beta"]
rom = "out/beta.bin"
sha1 = "{ROM_SHA1}"
"""

# Writes the same bytes for every version, and records each invocation for assertions.
BUILD_SH = """mkdir -p out
printf 'fake rom contents\\n' > "out/$1.bin"
echo "$1" >> "${BUILD_LOG:-/dev/null}"
"""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, timeout=60
    )
    return result.stdout.strip()


def make_target_repo(root: Path, *, install_hook: bool = True) -> tuple[Path, str]:
    """A git repo with a valid .rematch/ dir, a disabled upstream push URL and the hook installed.

    Returns (repo path, pinned commit sha).
    """
    repo = root / "demo"
    (repo / ".rematch").mkdir(parents=True)
    (repo / ".rematch" / "target.toml").write_text(TARGET_TOML)
    (repo / ".rematch" / "pre-push").write_text(PRE_PUSH)
    (repo / "build.sh").write_text(BUILD_SH)
    (repo / ".gitignore").write_text("out/\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "upstream", f"https://{FORBIDDEN}demo.git")
    git(repo, "remote", "set-url", "--push", "upstream", "DISABLED")
    if install_hook:
        hook = repo / ".git" / "hooks" / "pre-push"
        shutil.copy(repo / ".rematch" / "pre-push", hook)
        hook.chmod(0o755)
    return repo, git(repo, "rev-parse", "HEAD")
