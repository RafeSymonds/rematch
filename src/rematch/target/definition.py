"""`.rematch/target.toml` model and loader (SPEC §2.1), plus the adapter built from it."""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from rematch.toolchain.runner import ToolchainRunner

TARGET_DIR_NAME = ".rematch"
TARGET_TOML = "target.toml"


class TargetDefError(Exception):
    """target.toml is missing, unreadable or invalid."""


class BuildError(Exception):
    """A version's build command failed."""


def _check_relative(path: str) -> str:
    parts = PurePosixPath(path).parts
    if not path or PurePosixPath(path).is_absolute() or ".." in parts:
        raise ValueError(f"must be a relative path without '..': {path!r}")
    return path


class VersionDef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    build: list[str] = Field(min_length=1)
    rom: str
    sha1: str = Field(pattern=r"^[0-9a-f]{40}$")
    build_dir: str | None = None  # Phase 1: holds compile_commands and the map file

    @field_validator("rom", "build_dir")
    @classmethod
    def _rom_relative(cls, value: str | None) -> str | None:
        return None if value is None else _check_relative(value)


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BuildSection(_Section):
    compile_commands: str = "compile_commands.json"  # relative to the version's build_dir
    map: str  # linker map, relative to the version's build_dir
    map_format: Literal["mwld-xmap"]
    # Untracked paths (globs, relative to the repo) a fresh worktree needs before it can build,
    # such as fetched toolchains or dependencies. Copied from the live worktree (§10).
    worktree_copy: list[str] = []
    # Header directories (relative to the repo) that integration may edit: a rematch:protos
    # block replaces a placeholder prototype there (§10). Empty: prototypes can't be overridden.
    header_dirs: list[str] = []

    @field_validator("compile_commands", "map")
    @classmethod
    def _relative(cls, value: str) -> str:
        return _check_relative(value)

    @field_validator("worktree_copy", "header_dirs")
    @classmethod
    def _relative_all(cls, value: list[str]) -> list[str]:
        return [_check_relative(v) for v in value]


class SourceSection(_Section):
    asm: list[str] = []  # globs of assembly TUs
    c: list[str] = []  # globs of C TUs
    exclude: list[str] = []  # globs excluded from inventory (status 'skipped')
    include_dirs: list[str] = []  # header index roots
    version_defines: list[str] = []  # macros whose #if blocks make a TU version-conditional


class LinkOrderSection(_Section):
    file: str
    format: Literal["nitro-lsf"]
    # Older names of the link-order file, tried in order at commits where `file` is missing
    # (history, §11.1). They may list objects (`x.o`) instead of sources.
    history: list[str] = []

    @field_validator("file")
    @classmethod
    def _relative(cls, value: str) -> str:
        return _check_relative(value)

    @field_validator("history")
    @classmethod
    def _relative_all(cls, value: list[str]) -> list[str]:
        return [_check_relative(v) for v in value]


class AsmSection(_Section):
    format: Literal["pret-macros"]
    # Version whose addresses the assembly's address comments use (history, §11.1).
    address_version: str | None = None


class WorkerSection(_Section):
    intro: str = ""  # target sentence for the worker system prompt (Appendix A {{ target_intro }})
    compiler: str = ""  # compiler family for shared learnings: docs/learnings/<compiler>.json
    # Header (relative to .rematch/) that learnings evidence includes as "types.h": typedefs
    # plus any pragmas the target's code always compiles under. Default: rematch's types.h.
    trial_header: str = ""
    # ISA mode -> (prologue, epilogue) lines the harness puts around a function of that mode
    # when the target's headers set another one, e.g. {arm = ["#pragma thumb off",
    # "#pragma thumb on"]}. Used by the check and by integrated C pieces.
    mode_wrap: dict[str, tuple[str, str]] = {}


class ContextSection(_Section):
    # argv run through TOOLCHAIN_EXEC in the vanilla worktree; prints a flattened header context.
    # Placeholders: {target_dir} (.rematch/ of TARGET_REPO), {version}, {input} (stub .c path).
    command: list[str] = Field(min_length=1)


class NamingSection(_Section):
    # str.format templates; fields: address (int), overlay (int, overlays only), module (str).
    anonymous: str = "sub_{address:08X}"
    overlay_anonymous: str = "sub_{address:08X}"


class TargetDef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Used in file names (the default DB path), so keep it filesystem-safe.
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    default_versions: list[str] = Field(min_length=1)
    required_paths: list[str] = []
    versions: dict[str, VersionDef]
    # Phase 1 sections. Optional so a Phase 0 target still validates; commands that need one
    # fail with a clear message (TargetDef.require).
    build_info: BuildSection | None = Field(default=None, alias="build")
    source: SourceSection | None = None
    link_order: LinkOrderSection | None = None
    asm: AsmSection | None = None
    context: ContextSection | None = None
    naming: NamingSection = NamingSection()
    worker: WorkerSection = WorkerSection()

    @field_validator("required_paths")
    @classmethod
    def _paths_relative(cls, value: list[str]) -> list[str]:
        return [_check_relative(p) for p in value]

    @model_validator(mode="after")
    def _versions_defined(self) -> TargetDef:
        missing = [v for v in self.default_versions if v not in self.versions]
        if missing:
            raise ValueError(f"default_versions without a [versions.<name>] table: {missing}")
        if self.build_info is not None:
            no_dir = [name for name, v in self.versions.items() if v.build_dir is None]
            if no_dir:
                raise ValueError(f"[build] requires build_dir in every version: {no_dir}")
        return self

    def require[T](self, section: T | None, name: str) -> T:
        """Return a Phase 1+ section or fail with a message naming the missing table."""
        if section is None:
            raise TargetDefError(f"target.toml has no [{name}] section, which this command needs")
        return section

    def build_dir(self, worktree: Path, version: str) -> Path:
        rel = self.versions[version].build_dir
        if rel is None:
            raise TargetDefError(f"target.toml [versions.{version}] has no build_dir")
        return worktree / rel


def target_dir(target_repo: Path) -> Path:
    return target_repo / TARGET_DIR_NAME


def load_target_def(target_repo: Path) -> TargetDef:
    """Read `<target_repo>/.rematch/target.toml` from the repo's working tree."""
    path = target_dir(target_repo) / TARGET_TOML
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TargetDefError(f"{path} not found") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TargetDefError(f"{path}: {exc}") from exc
    try:
        return TargetDef.model_validate(raw)
    except ValidationError as exc:
        raise TargetDefError(f"{path}: {exc}") from exc


class TomlTarget:
    """TargetAdapter backed by target.toml. Builds run through the toolchain runner."""

    def __init__(
        self,
        definition: TargetDef,
        runner: ToolchainRunner,
        build_timeout: float,
        version_override: list[str] | None = None,
    ) -> None:
        unknown = [v for v in version_override or [] if v not in definition.versions]
        if unknown:
            raise TargetDefError(f"TARGET_VERSIONS names versions not in target.toml: {unknown}")
        self._def = definition
        self._runner = runner
        self._build_timeout = build_timeout
        self._versions = list(version_override or definition.default_versions)

    @property
    def name(self) -> str:
        return self._def.name

    @property
    def definition(self) -> TargetDef:
        return self._def

    def versions(self) -> list[str]:
        return list(self._versions)

    def expected_sha1(self, version: str) -> str:
        return self._def.versions[version].sha1

    def rom_path(self, worktree: Path, version: str) -> Path:
        return worktree / self._def.versions[version].rom

    def build(self, worktree: Path, version: str) -> Path:
        argv = self._def.versions[version].build
        result = self._runner.run(argv, cwd=worktree, timeout=self._build_timeout)
        if result.returncode != 0:
            tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-20:])
            raise BuildError(f"{' '.join(argv)} exited {result.returncode}:\n{tail}")
        return self.rom_path(worktree, version)
