"""`rematch doctor` checks (SPEC §13, §6.2, §4, §2.3). Spends no model tokens."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from rematch.config import Settings
from rematch.db import store
from rematch.target.definition import (
    PRE_PUSH_HOOK,
    BuildError,
    TargetDefError,
    TomlTarget,
    load_target_def,
    target_dir,
)
from rematch.toolchain.runner import ToolchainRunner

MIN_CLAUDE_VERSION = (2, 1, 259)
AGENT_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude")
GIT_TIMEOUT_SEC = 60
WORKTREE_ADD_TIMEOUT_SEC = 600

Log = Callable[[str], None]


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _git(
    repo: Path, *args: str, timeout: float = GIT_TIMEOUT_SEC
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# --- Claude Code version -------------------------------------------------------------


def parse_claude_version(output: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def check_claude_version(claude_bin: str) -> CheckResult:
    name = "claude version"
    try:
        result = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(name, False, f"cannot run {claude_bin} --version: {exc}")
    version = parse_claude_version(result.stdout)
    if result.returncode != 0 or version is None:
        return CheckResult(name, False, f"unrecognized output: {result.stdout.strip()!r}")
    wanted = ".".join(map(str, MIN_CLAUDE_VERSION))
    found = ".".join(map(str, version))
    if version < MIN_CLAUDE_VERSION:
        return CheckResult(name, False, f"{found} < {wanted}")
    return CheckResult(name, True, f"{found} >= {wanted}")


# --- Workdir ancestors ---------------------------------------------------------------


def claude_config_dir() -> Path:
    """Claude Code's user config dir, exempt from the ancestor check (SPEC §4)."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def find_agent_files(workdir: Path, exempt: Path) -> list[Path]:
    """Agent instruction files in `workdir` or any ancestor, except `exempt`."""
    start = workdir.expanduser().resolve()
    exempt_resolved = exempt.expanduser().resolve()
    hits: list[Path] = []
    for directory in [start, *start.parents]:
        for name in AGENT_FILES:
            candidate = directory / name
            if not (candidate.exists() or candidate.is_symlink()):
                continue
            if candidate.resolve() == exempt_resolved:
                continue
            hits.append(candidate)
    return hits


def check_workdir(workdir: Path, exempt: Path) -> CheckResult:
    name = "workdir isolation"
    hits = find_agent_files(workdir, exempt)
    if hits:
        return CheckResult(
            name, False, "agent files above REMATCH_WORKDIR: " + ", ".join(map(str, hits))
        )
    return CheckResult(name, True, f"{workdir} (exempt: {exempt})")


# --- Push guard ----------------------------------------------------------------------


def _hooks_dir(repo: Path) -> Path | None:
    hooks_path = _git(repo, "config", "--get", "core.hooksPath").stdout.strip()
    if hooks_path:
        path = Path(hooks_path).expanduser()
        return path if path.is_absolute() else repo / path
    common = _git(repo, "rev-parse", "--git-common-dir")
    if common.returncode != 0:
        return None
    common_dir = Path(common.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = repo / common_dir
    return common_dir / "hooks"


def check_push_guard(repo: Path, forbidden: list[str]) -> CheckResult:
    name = "push guard"
    if not forbidden:
        return CheckResult(name, True, "target.toml lists no forbidden_push_urls")
    remotes = _git(repo, "remote")
    if remotes.returncode != 0:
        return CheckResult(name, False, f"git remote failed: {remotes.stderr.strip()}")
    bad: list[str] = []
    for remote in remotes.stdout.split():
        urls = _git(repo, "remote", "get-url", "--push", "--all", remote).stdout.split()
        bad += [f"{remote} → {url}" for url in urls if any(p in url for p in forbidden)]
    if bad:
        return CheckResult(name, False, "push URL matches forbidden_push_urls: " + "; ".join(bad))
    source = target_dir(repo) / PRE_PUSH_HOOK
    if not source.is_file():
        return CheckResult(name, False, f"forbidden_push_urls set but {source} is missing")
    hooks = _hooks_dir(repo)
    if hooks is None:
        return CheckResult(name, False, f"cannot locate the git hooks dir of {repo}")
    installed = hooks / "pre-push"
    fix = f"install with: cp {source} {installed} && chmod +x {installed}"
    if not installed.is_file():
        return CheckResult(name, False, f"pre-push hook not installed; {fix}")
    if not os.access(installed, os.X_OK):
        return CheckResult(name, False, f"{installed} is not executable")
    if installed.read_bytes() != source.read_bytes():
        return CheckResult(name, False, f"{installed} differs from {source}; {fix}")
    return CheckResult(name, True, f"no forbidden push URLs; hook installed at {installed}")


# --- Vanilla worktree ----------------------------------------------------------------


def vanilla_worktree_path(repo: Path) -> Path:
    return repo.parent / f"{repo.name}-vanilla"


def ensure_vanilla_worktree(repo: Path, pin: str, log: Log) -> tuple[Path, CheckResult]:
    """Create the vanilla worktree at `pin` if missing, then require HEAD == pin and no edits."""
    name = "vanilla worktree"
    path = vanilla_worktree_path(repo)
    resolved = _git(repo, "rev-parse", "--verify", "--quiet", f"{pin}^{{commit}}")
    if resolved.returncode != 0:
        return path, CheckResult(name, False, f"TARGET_PIN {pin!r} is not a commit in {repo}")
    sha = resolved.stdout.strip()
    if not path.exists():
        log(f"creating vanilla worktree {path} at {sha[:12]}")
        added = _git(
            repo, "worktree", "add", "--detach", str(path), sha, timeout=WORKTREE_ADD_TIMEOUT_SEC
        )
        if added.returncode != 0:
            return path, CheckResult(
                name, False, f"git worktree add failed: {added.stderr.strip()}"
            )
    head = _git(path, "rev-parse", "HEAD")
    if head.returncode != 0:
        return path, CheckResult(name, False, f"{path} is not a git worktree")
    if head.stdout.strip() != sha:
        return path, CheckResult(
            name, False, f"{path} HEAD is {head.stdout.strip()[:12]}, TARGET_PIN is {sha[:12]}"
        )
    status = _git(path, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0 or status.stdout.strip():
        return path, CheckResult(name, False, f"{path} has tracked changes:\n{status.stdout}")
    return path, CheckResult(name, True, f"{path} at {sha[:12]}, clean")


# --- Toolchain, builds, required paths, DB -------------------------------------------


def check_toolchain_exec(runner: ToolchainRunner, cwd: Path) -> CheckResult:
    name = "toolchain exec"
    if not runner.prefix:
        return CheckResult(name, True, "TOOLCHAIN_EXEC not set; toolchain runs on the host")
    try:
        result = runner.run(["true"], cwd=cwd, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(name, False, f"{' '.join(runner.argv(['true'], cwd))}: {exc}")
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return CheckResult(name, False, f"{' '.join(runner.argv(['true'], cwd))}: {detail}")
    return CheckResult(name, True, " ".join(runner.prefix))


def check_workdir_visible(runner: ToolchainRunner, workdir: Path) -> CheckResult:
    """Sandboxes are compiled through TOOLCHAIN_EXEC, so the toolchain must see REMATCH_WORKDIR."""
    name = "toolchain sees workdir"
    workdir = workdir.expanduser()
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        result = runner.run(["test", "-d", str(workdir)], cwd=workdir, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(name, False, f"{workdir}: {exc}")
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return CheckResult(name, False, f"{workdir} not visible to the toolchain: {detail}")
    return CheckResult(name, True, str(workdir))


def check_tool(name: str, argv: list[str], must_contain: str | None = None) -> CheckResult:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(name, False, f"cannot run {argv[0]}: {exc}")
    first = (result.stdout or result.stderr).strip().splitlines()[:1]
    version = first[0] if first else ""
    if result.returncode != 0:
        return CheckResult(name, False, f"{argv[0]} exited {result.returncode}")
    if must_contain is not None and must_contain not in result.stdout:
        return CheckResult(name, False, f"{argv[0]} is not {must_contain}: {version!r}")
    return CheckResult(name, True, version)


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_version(target: TomlTarget, worktree: Path, version: str, log: Log) -> CheckResult:
    name = f"build {version}"
    rom = target.rom_path(worktree, version)
    if not rom.is_file():
        log(f"building {version} in {worktree} (ROM missing)")
        try:
            target.build(worktree, version)
        except (BuildError, OSError, subprocess.TimeoutExpired) as exc:
            return CheckResult(name, False, str(exc))
        if not rom.is_file():
            return CheckResult(name, False, f"build succeeded but {rom} does not exist")
    actual = sha1_file(rom)
    expected = target.expected_sha1(version)
    if actual != expected:
        return CheckResult(name, False, f"{rom} sha1 {actual}, expected {expected}")
    return CheckResult(name, True, f"{rom.relative_to(worktree)} sha1 {actual}")


def check_required_paths(worktree: Path, paths: list[str]) -> CheckResult:
    name = "required paths"
    missing = [p for p in paths if not (worktree / p).exists()]
    if missing:
        return CheckResult(name, False, "missing in vanilla worktree: " + ", ".join(missing))
    return CheckResult(name, True, f"{len(paths)} present")


def check_db(path: Path) -> CheckResult:
    name = "database"
    try:
        conn = store.connect(path)
    except (OSError, sqlite3.Error, store.MigrationError) as exc:
        return CheckResult(name, False, f"{path}: {exc}")
    try:
        version = store.schema_version(conn)
    finally:
        conn.close()
    latest = store.latest_version()
    if version != latest:
        return CheckResult(name, False, f"{path} at schema {version}, expected {latest}")
    return CheckResult(name, True, f"{path} at schema {version}")


# --- Orchestration -------------------------------------------------------------------


def iter_doctor(settings: Settings, log: Log = lambda _msg: None) -> Iterator[CheckResult]:
    """Yield each check's result as it completes. Later checks are skipped if prerequisites fail."""
    yield check_claude_version(settings.claude_bin)
    yield check_workdir(settings.rematch_workdir, claude_config_dir())

    repo = settings.target_repo
    try:
        definition = load_target_def(repo)
        runner = ToolchainRunner.from_spec(settings.toolchain_exec)
        target = TomlTarget(
            definition, runner, settings.build_timeout_sec, settings.version_override()
        )
    except (TargetDefError, ValueError) as exc:
        yield CheckResult("target.toml", False, str(exc))
        return
    yield CheckResult("target.toml", True, f"{definition.name}: versions {target.versions()}")

    yield check_push_guard(repo, definition.forbidden_push_urls)

    worktree, worktree_result = ensure_vanilla_worktree(repo, settings.target_pin, log)
    yield worktree_result
    if worktree_result.ok:
        toolchain = check_toolchain_exec(runner, worktree)
        yield toolchain
        if toolchain.ok:
            yield check_workdir_visible(runner, settings.rematch_workdir)
            for version in target.versions():
                yield check_version(target, worktree, version, log)
            yield check_required_paths(worktree, definition.required_paths)

    yield check_tool("objdump", [settings.objdump, "--version"])
    yield check_tool("ctags", [settings.ctags, "--version"], must_contain="Universal Ctags")
    yield check_db(settings.db_path(definition.name))
