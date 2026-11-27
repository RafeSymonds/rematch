"""Configuration loaded from the environment and `.env` (SPEC §4)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every SPEC §4 key. Environment variables override `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    target_repo: Path
    target_pin: str = Field(min_length=1)
    target_versions: str = ""
    toolchain_exec: str = ""
    build_timeout_sec: float = Field(default=3600, gt=0)

    rematch_db: Path | None = None
    rematch_var: Path = Path("./var")
    rematch_workdir: Path = Path("~/.rematch/work")

    worker_count: int = Field(default=4, ge=1)
    claude_bin: str = "claude"
    worker_auth_mode: Literal["subscription", "api"] = "subscription"
    anthropic_api_key: SecretStr = SecretStr("")
    worker_model: str = "sonnet"
    worker_effort: str = "high"
    # Effort per size bucket (S/M/L, SPEC §11.2) when --effort isn't given; empty = WORKER_EFFORT.
    effort_by_size: str = "S:medium,M:high,L:high"
    escalation_ladder: str = "sonnet:high,sonnet:xhigh,opus:high,opus:xhigh"
    max_turns: int = Field(default=60, ge=1)
    max_checks: int = Field(default=40, ge=1)
    stagnation_limit: int = Field(default=8, ge=1)
    attempt_timeout_sec: float = Field(default=1200, gt=0)
    max_budget_usd_per_attempt: float = Field(default=3.00, gt=0)
    max_attempts_per_function: int = Field(default=4, ge=1)
    retry_cooldown_min: int = Field(default=240, ge=0)
    examples_k: int = Field(default=3, ge=0)
    # After a run, suggest `rematch learnings curate` once this many candidates are pending.
    curate_pending_threshold: int = Field(default=50, ge=1)
    examples_mode: Literal["naive", "similar"] = "similar"  # SPEC §6.7; naive = Phase 2 baseline
    reference_repos: str = ""
    diff_backend: Literal["objdiff", "objdump"] = "objdump"
    objdiff_bin: str = "objdiff-cli"
    objdump: str = "objdump"
    m2c: str = ""  # path to m2c.py for draft.c seeding (Phase 3 B2); empty = no drafts
    ctags: str = "ctags"
    bench_cutoff_date: str = "2026-07-01"
    compile_timeout_sec: float = Field(default=20, gt=0)
    log_level: str = "info"

    @field_validator("rematch_db", mode="before")
    @classmethod
    def _empty_db_is_unset(cls, value: object) -> object:
        # `.env.example` ships `REMATCH_DB=`; an empty value means "use the default".
        return None if value == "" else value

    @field_validator("target_repo", "rematch_db", "rematch_var", "rematch_workdir", mode="after")
    @classmethod
    def _expand_user(cls, value: Path | None) -> Path | None:
        # Absolute from here on: these paths are handed to processes running in sandboxes.
        return None if value is None else value.expanduser().resolve()

    def effort_for_size(self, size_bucket: str) -> str:
        table = dict(part.split(":", 1) for part in self.effort_by_size.split(",") if ":" in part)
        return table.get(size_bucket, self.worker_effort)

    def version_override(self) -> list[str]:
        """`TARGET_VERSIONS` as a list. Empty means use target.toml `default_versions`."""
        return [v.strip() for v in self.target_versions.split(",") if v.strip()]

    def db_path(self, target_name: str) -> Path:
        """`REMATCH_DB`, defaulting to `<REMATCH_VAR>/<target name>.sqlite`."""
        if self.rematch_db is not None:
            return self.rematch_db
        return self.rematch_var / f"{target_name}.sqlite"
