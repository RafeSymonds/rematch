# SPEC: rematch (authoritative)

This file is the source of truth for structure, contracts and procedures. If code and SPEC disagree, either fix the code or update SPEC in the same change, and say which in the commit message. Items marked **VERIFY** are assumptions that must be confirmed (and this file updated) in the phase noted. Target-specific facts and their VERIFY items live in the target's `TARGET.md` (§2), never here.

---

## 1. Terms

| Term | Meaning |
|---|---|
| **Target** | A matching-decompilation repo that rematch works on, described by its own `.rematch/` directory (§2). Usually a private fork of a public decomp project. |
| **Version** | One build variant of the target (for example two game versions or regions built from the same tree). Each version has its own expected binary hash. |
| **TU** | Translation unit: one assembly or C source file linked as one object, as listed in the target's link-order file. |
| **Vanilla build** | An unmodified build of the target at a pinned commit, used as the answer key. Lives in the vanilla worktree (§6.2). |
| **Attempt** | One worker session on one function (or one TU data task). |
| **Match** | Candidate object code for the symbol is identical to the vanilla object (§6.5). |
| **Bench / live** | Bench: target is a human-decompiled function with its C hidden. Live: target is a function still in assembly. |

---

## 2. Target definition

rematch has no per-target code paths. Everything it knows about a target comes from `<TARGET_REPO>/.rematch/`, read from TARGET_REPO's working tree (not from the vanilla worktree, so editing it never requires a re-pin).

```
<TARGET_REPO>/.rematch/
  target.toml      # machine-readable definition (below)
  TARGET.md        # target facts, target-level VERIFY items, progress
  pre-push         # git hook that rejects pushes to forbidden_push_urls
  Dockerfile       # optional: build image for hosts that can't run the toolchain natively (§4 TOOLCHAIN_EXEC)
  verify-log.md    # evidence for resolved target VERIFY items
```

### 2.1 `target.toml` (Phase 0 fields)

```toml
name = "example"
default_versions = ["us", "jp"]            # first is primary for diffs
forbidden_push_urls = ["github.com/upstream-org/"]
required_paths = ["tools/cc/bin/cc"]       # must exist in the vanilla worktree after the build

[versions.us]
build = ["make", "us"]                     # argv, run in the vanilla worktree through TOOLCHAIN_EXEC
rom = "build/us/game.us.bin"               # relative to the worktree
sha1 = "0123456789abcdef0123456789abcdef01234567"
```

Validation: every name in `default_versions` has a `[versions.<name>]` table; `sha1` is 40 lowercase hex characters; `rom` and `required_paths` are relative and contain no `..`; `build` is a non-empty argv list. Later phases add sections and update this section when they do.

### 2.1.1 Phase 1 fields

```toml
[versions.us]
build_dir = "build/us"                     # holds compile_commands.json and the map file

[build]
compile_commands = "compile_commands.json" # relative to build_dir (default shown)
map = "main.map"                           # linker map, relative to build_dir
map_format = "mwld-xmap"                   # Metrowerks linker xMAP

[source]
asm = ["asm/**/*.s"]                       # globs of assembly TUs
c = ["src/**/*.c"]                         # globs of C TUs
exclude = ["lib/**"]                       # inventoried as status 'skipped'
include_dirs = ["include"]                 # header index roots
version_defines = ["US", "JP"]             # macros whose #if blocks make a TU version-conditional

[link_order]
file = "link.lsf"
format = "nitro-lsf"                       # NitroSDK LSF: Static/Autoload/Overlay blocks of Object lines

[asm]
format = "pret-macros"                     # `thumb_func_start NAME` / `NAME: ; 0xADDR` / `_ADDR: .word X`

[context]
command = ["sh", "{target_dir}/make_ctx.sh", "{version}", "{input}"]

[naming]
anonymous = "sub_{address:08X}"            # str.format; fields: address, overlay (overlays only), module
overlay_anonymous = "ov{overlay:02d}_{address:08X}"
```

Phase 1 sections are optional for validation, and the commands that need one fail with a message naming the missing table. `[build]` requires `build_dir` in every version. Formats are names of parsers implemented in rematch for toolchain and ecosystem formats (never per-game code). Adding a format means adding a parser with fixture tests. The `[context]` command runs through `TOOLCHAIN_EXEC` with cwd = the vanilla worktree, receives a stub `.c` containing `#include` lines as `{input}`, and prints a flattened context (types, prototypes and macro definitions) on stdout.

### 2.2 `TARGET.md`

Before Phase 1 starts, TARGET.md must state, with values taken from the vanilla build (`build.ninja`, `compile_commands.json`, the map file) and never guessed:
- build commands, build directories and output binaries per version, with expected hashes
- compiler, assembler and linker, and how they're invoked (wrapper, emulation layer)
- global compile and assembly flags, and per-version defines
- ISA modes in use (for example ARM/Thumb) and how each function's mode is determined
- assembly function shape, address labels and literal pool format
- the link-order file, what it generates, and which generated files must never be edited
- object file paths for assembly and C TUs inside the build dir
- the linker map file location
- version conditionals in assembly and C
- the upstream project's contribution policy
- an inventory snapshot (function counts in assembly and C), used by the Phase 1 exit criterion

### 2.3 `pre-push`

The target's hook rejects any push whose remote URL contains one of `forbidden_push_urls`. It is installed into the target repo's common git dir (`git rev-parse --git-common-dir`/hooks/pre-push), which covers every worktree. `rematch doctor` checks that the installed hook is byte-identical to `.rematch/pre-push`.

---

## 3. Harness repo layout

```
rematch/
  pyproject.toml                 # uv-managed; Python >=3.12
  .env.example
  src/rematch/
    cli.py                       # Typer app, entrypoint `rematch`
    config.py                    # pydantic-settings, loads .env
    doctor.py                    # `rematch doctor` checks (§13)
    db/schema.sql  db/store.py  db/migrations/
    target/base.py               # TargetAdapter protocol (§6.0)
    target/definition.py         # target.toml model and loader (§2.1)
    inventory/asm_parser.py  inventory/c_parser.py  inventory/mapfile.py  inventory/history.py
    context/header_index.py  context/ctx_builder.py  context/similarity.py  context/examples.py
    toolchain/runner.py          # runs target toolchain commands through TOOLCHAIN_EXEC
    toolchain/compile.py  toolchain/objects.py
    diff/match.py  diff/objdump_backend.py  diff/score.py  diff/hints.py  diff/render.py
    toolchain/compile_db.py      # compile_commands.json: argv and object path per TU
    session.py                   # settings + target + vanilla worktree for target commands
    worker/sandbox.py  worker/task.py  worker/check_entry.py  worker/roundtrip.py  worker/invoke.py  worker/outcome.py
    orchestrator/scheduler.py  orchestrator/leases.py  orchestrator/escalation.py  orchestrator/run.py
    integrate/tu_builder.py  integrate/lsf.py  integrate/verify.py
    bench/pool.py  bench/sets.py  bench/report.py
    lint/readability.py
    learnings/candidates.py  learnings/curate.py
    selftest/oracle_agent.py  selftest/saboteur_agent.py
  prompts/
    worker_system.md             # Appendix A (versioned: header line `prompt_version: N`)
    task.md.j2                   # Appendix B
    tu_data_task.md.j2
    curate.md
  docs/
    benchmarks.md                # append-only benchmark log (CONTRIBUTING.md)
    LEARNINGS.md                 # curated compiler quirks, injected into sandboxes
    verify-log.md                # evidence for resolved rematch VERIFY items
  tests/
  var/                           # gitignored: db, logs, bench sets (sandboxes live in REMATCH_WORKDIR, outside the repo)
```

`.gitignore` must include: `var/`, `.env`, ROM and save extensions (`*.nds`, `*.gba`, `*.gb`, `*.gbc`, `*.z64`, `*.n64`, `*.v64`, `*.iso`, `*.sav`), `bios*.bin`, `*bios7*`, `*bios9*`, `firmware*.bin`.

This repo is target-agnostic: no game names, target paths, host names or personal setup. Those belong in the target's `.rematch/` or in the gitignored `.env`.

---

## 4. Configuration (`.env`)

| Key | Default | Meaning |
|---|---|---|
| `TARGET_REPO` | (required) | Path to the target repo (a fork or clone you control) |
| `TARGET_PIN` | (required) | Commit used for the vanilla build (answer key) |
| `TARGET_VERSIONS` | target.toml `default_versions` | Comma-separated override. The first one is primary for diffs. |
| `TOOLCHAIN_EXEC` | (empty) | Argv prefix for every target toolchain command (build, compile), split with shell-style quoting. `{cwd}` is replaced with the command's working directory. Empty means run on the host. Example: `docker exec -i -w {cwd} <container>` with the repos mounted at the same paths inside the container. |
| `BUILD_TIMEOUT_SEC` | `3600` | Per-version vanilla build timeout |
| `REMATCH_DB` | `./var/<target name>.sqlite` | |
| `REMATCH_VAR` | `./var` | DB, logs, bench sets |
| `REMATCH_WORKDIR` | `~/.rematch/work` | Sandboxes. **Must be outside any repo**: no `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md` or `.claude/` in it or any ancestor (§7.1). The one exemption is Claude Code's user config dir (`$CLAUDE_CONFIG_DIR`, default `~/.claude`), which exists on every host and whose leakage is handled by §7.3. `rematch doctor` enforces this. |
| `WORKER_COUNT` | `4` | Concurrent attempts |
| `CLAUDE_BIN` | `claude` | |
| `WORKER_AUTH_MODE` | `subscription` | `subscription` or `api` (§7.3) |
| `ANTHROPIC_API_KEY` | (empty) | API mode only |
| `WORKER_MODEL` | `sonnet` | Default model alias |
| `WORKER_EFFORT` | `high` | |
| `ESCALATION_LADDER` | `sonnet:high,sonnet:xhigh,opus:high,opus:xhigh` | §8.4 |
| `MAX_TURNS` | `60` | Passed as `--max-turns` |
| `MAX_CHECKS` | `40` | Enforced by `./check` |
| `STAGNATION_LIMIT` | `8` | Consecutive checks without a new best score |
| `ATTEMPT_TIMEOUT_SEC` | `1200` | Wall clock. SIGINT, then SIGTERM after 30 s. |
| `MAX_BUDGET_USD_PER_ATTEMPT` | `3.00` | API mode only (`--max-budget-usd`) |
| `MAX_ATTEMPTS_PER_FUNCTION` | `4` | Before status `stuck` |
| `RETRY_COOLDOWN_MIN` | `240` | |
| `EXAMPLES_K` | `3` | Similar matched functions per sandbox |
| `REFERENCE_REPOS` | (empty) | Comma-separated paths to related decomp repos (same engine or compiler) for cross-repo examples (Phase 3) |
| `DIFF_BACKEND` | `objdump` | `objdump` or `objdiff` (§6.5) |
| `OBJDIFF_BIN` | `objdiff-cli` | |
| `OBJDUMP` | `objdump` | Disassembler for the target's architecture (for example `arm-none-eabi-objdump`) |
| `CTAGS` | `ctags` | Universal Ctags, used by the header index (§6.3) |
| `BENCH_CUTOFF_DATE` | `2026-07-01` | Contamination cutoff (§11.1) |
| `COMPILE_TIMEOUT_SEC` | `20` | |
| `LOG_LEVEL` | `info` | |

---

## 5. Data model (SQLite)

One database per target (`REMATCH_DB`).

```sql
CREATE TABLE functions (
  id              INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,            -- current name in target
  address         INTEGER NOT NULL,
  module          TEXT NOT NULL,            -- module or overlay name as the target defines it
  tu_path         TEXT NOT NULL,            -- e.g. 'asm/foo.s' or 'src/foo.c'
  source_kind     TEXT NOT NULL CHECK (source_kind IN ('asm','c_human')),
  mode            TEXT NOT NULL,            -- ISA mode as the target defines it, e.g. 'thumb', 'arm'
  n_insns         INTEGER,
  n_branches      INTEGER,
  has_switch      INTEGER DEFAULT 0,
  pool_entries    INTEGER,
  callees_json    TEXT,                     -- ["memset", "sub_02096780", ...]
  version_cond    INTEGER DEFAULT 0,        -- TU contains version conditionals
  difficulty      REAL,
  status          TEXT NOT NULL DEFAULT 'untried'
                  CHECK (status IN ('untried','leased','matched','failed','stuck',
                                    'integrated','superseded','skipped')),
  escalation      INTEGER NOT NULL DEFAULT 0,
  attempts        INTEGER NOT NULL DEFAULT 0,
  best_score      REAL,
  decompiled_at   TEXT,                     -- c_human only: ISO date it left assembly (§11.1)
  lease_owner     TEXT, lease_expires TEXT,
  updated_at      TEXT NOT NULL,
  UNIQUE (module, address)
);

CREATE TABLE runs (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('bench','live','selftest')),
  bench_set   TEXT,
  config_json TEXT NOT NULL,                -- full resolved config + prompt_version + git SHAs + target.toml hash
  started_at  TEXT NOT NULL, ended_at TEXT, notes TEXT
);

CREATE TABLE attempts (
  id             INTEGER PRIMARY KEY,
  run_id         INTEGER NOT NULL REFERENCES runs(id),
  function_id    INTEGER REFERENCES functions(id),
  task_kind      TEXT NOT NULL CHECK (task_kind IN ('function','tu_data','tu_fix')),
  model          TEXT, effort TEXT, escalation INTEGER,
  prompt_version INTEGER, context_version INTEGER,
  started_at     TEXT, ended_at TEXT,
  outcome        TEXT CHECK (outcome IN ('match','nonmatch','compile_fail','stagnation',
                                         'check_limit','turn_limit','timeout','budget',
                                         'invalid','error')),
  checks_used    INTEGER, best_score REAL, final_score REAL,
  num_turns      INTEGER, input_tokens INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cost_usd REAL,  -- client-side estimate from result message
  sandbox_path   TEXT, transcript_path TEXT, best_candidate_path TEXT
);

CREATE TABLE matches (
  function_id      INTEGER PRIMARY KEY REFERENCES functions(id),
  attempt_id       INTEGER NOT NULL REFERENCES attempts(id),
  c_source         TEXT NOT NULL,
  verified_versions TEXT NOT NULL,           -- comma-separated version names
  lint_json        TEXT NOT NULL,            -- §9 results (must be all-pass)
  verified_at      TEXT NOT NULL,
  integrated_commit TEXT
);

CREATE TABLE learning_candidates (
  id INTEGER PRIMARY KEY, attempt_id INTEGER REFERENCES attempts(id),
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
  created_at TEXT NOT NULL
);

CREATE TABLE bench_sets (
  id TEXT PRIMARY KEY,                       -- e.g. 'b100-s42-c20260701'
  function_ids_json TEXT NOT NULL, seed INTEGER, cutoff TEXT, created_at TEXT NOT NULL
);
```

Migrations are forward-only SQL files in `db/migrations/NNNN_*.sql`, applied in order at startup. The number of the last applied migration is stored in `PRAGMA user_version`. `db/schema.sql` is the schema that applying every migration produces, and a test enforces that.

---

## 6. Core components

### 6.0 TargetAdapter (protocol)

Every target-specific behavior goes through this interface. All of it is implemented from `target.toml` (§2.1): compile argv and object paths come from `compile_commands.json`, and link order, assembly and map parsing use named format parsers in rematch. There is no per-target Python code (decided in Phase 1).

```python
class TargetAdapter(Protocol):
    name: str
    def versions(self) -> list[str]: ...
    def expected_sha1(self, version: str) -> str: ...
    def rom_path(self, worktree: Path, version: str) -> Path: ...
    def build(self, worktree: Path, version: str) -> Path: ...                  # returns ROM path
    def list_tus(self) -> list[TU]: ...                                         # from the link-order file, in link order
    def tu_object(self, tu: TU, version: str) -> Path: ...                      # vanilla build object
    def compile_command(self, tu_or_module: str, version: str) -> list[str]: ...  # exact argv, source path templated
    def include_dirs(self) -> list[Path]: ...
    def make_context(self, headers: list[str], version: str) -> str: ...        # flattened header context
    def swap_tu_to_c(self, tu: TU, c_path: Path) -> None: ...                   # edits the link-order source file
    def symbol_addresses(self, version: str) -> dict[str, tuple[str, int]]: ... # map file → (module, addr)
    def anonymous_name(self, module: str, address: int) -> str: ...             # §7.2, e.g. sub_<ADDR>
```

### 6.1 Inventory (`rematch inventory`)

- Functions come from the vanilla objects of the primary version: every sized `FUNC` symbol (mapping symbols excluded) of every TU in the link-order file, with its address from the linker map. Functions in an object but not in the map (dead-stripped) are logged and skipped.
- Features come from the normalized disassembly of the vanilla object (§6.5), for assembly and C alike: instruction count (data words excluded), conditional/unconditional branch count (calls and returns excluded), call targets (relocations on `bl`/`blx`), literal pool entries (data words), and `has_switch` (a computed jump that writes `pc`). Mode comes from the ARM mapping symbol (`$t`/`$a`) at the function start.
- The assembly parser (target format, TARGET.md) supplies each function's source text and address comment. A comment that disagrees with the map is logged, not fatal (it is usually another version's address).
- Module is the module or overlay containing the TU, from the link-order file.
- C TUs are `c_human`, assembly TUs `asm`. The C parser (`inventory/c_parser.py`) finds definitions and their bodies for round-trips and TU-local context (§6.4).
- Exclude library and SDK code in v1 (directories listed by the target): `status='skipped'`.
- Compute `difficulty` (§8.3). Inventory is idempotent and keyed by `(module, address)`. Renames upstream update `name` but keep the row.

### 6.2 Vanilla build and target objects

- `rematch doctor` keeps a dedicated worktree at `TARGET_REPO/../<repo>-vanilla` (never the live worktree). If it's missing, doctor creates it with `git worktree add --detach <path> TARGET_PIN`. It requires HEAD == `TARGET_PIN` and no tracked changes. For each version whose ROM is missing, it runs that version's `build` argv in the worktree through `TOOLCHAIN_EXEC` (timeout `BUILD_TIMEOUT_SEC`), then checks every version's hash.
- Target object for a function = `tu_object(tu, version)` from the vanilla worktree. Never rebuild vanilla objects during attempts.

### 6.3 Context builder

For each function, produce a sandbox `ctx.h`:

1. **Header index** (built once per pin): map every declared identifier (prototype, typedef, struct, union, enum, enumerator, macro constant, extern global) to its header file, using `ctags` or a clang-based parse over the target's include and source dirs.
2. Headers needed = the target's base header + the headers declaring every callee and every referenced symbol (call targets, pool symbol entries).
3. Flatten them via `make_context` (for example a target-provided m2ctx-style script run on a stub `.c` that includes those headers). Strip the target function's own prototype only if it would conflict. Keep it otherwise, because a correct prototype is legitimate info.
4. Bench mode: the context must not contain the target's body. Headers never contain bodies, but static inline functions defined in headers are fine.
5. Record `context_version` (bump whenever builder logic changes).

### 6.4 Compile-and-diff (`rematch cd`): the worker's only tool

Invocation inside a sandbox: `./check` (no arguments). It runs `rematch cd --sandbox <dir>` and prints the text rendering (§6.6). `cd` reads only the sandbox: `task.json` records the compile argv per version, the toolchain prefix, the disassembler and the limits, so `./check` needs no `.env`. Exit codes: 0 `MATCH`, 1 no match (including compile errors and rejections), 3 `STOP`.

Steps:
1. Read `candidate.c`. Reject immediately (status `rejected`, no compile; this counts as a check) if it contains (outside comments and string literals): `asm`, `__asm`, any `#pragma`, an `#include` of anything except `"ctx.h"`, no definition of the target function, or more than one.
2. Write scratch TU `scratch.<version>.c` = `#include "<ctx>"\n#line 1 "candidate.c"\n` + candidate. If the candidate defines the target `static`, append `void *const rematch_keep_<symbol> = (void *)<symbol>;`: nothing references the function in isolation, so the compiler would not emit it. The worker never sees this line.
3. **Isolated mode (default):** compile the scratch TU with the exact argv from the vanilla build's `compile_commands.json` entry for the TU (for an assembly TU: the first C entry with the same output directory), with only the source and output swapped, cwd = the entry's `directory`, through `TOOLCHAIN_EXEC`. Timeout `COMPILE_TIMEOUT_SEC`.
   - Context for a human-C target (bench, round-trip) = the flattened headers of the TU's `#include` lines, plus the TU's own declarations: its text without `#include` lines and without the target's definition, with `inline` bodies kept and every other body turned into a prototype. Other functions' `static` prototypes lose `static`, because mwcc rejects a call to a static function it never sees defined.
   - Context for an assembly target = the flattened base header plus the headers declaring its callees (header index).
4. **TU-context mode (escalation):** for `c_human` (bench) targets, compile a copy of the original TU with only the target body replaced. For `asm` targets (Phase 5), build a C file where neighbor functions are `GLOBAL_ASM` blocks via a tool such as asm_processor, if the target supports one. Codegen can depend on neighbors when the compiler does file-level interprocedural analysis or inlining.
5. Compile errors are returned as at most 15 lines, with paths stripped and line numbers relative to `candidate.c`. Some compilers (mwcc) report physical scratch lines despite `#line`, so `cd` maps them itself.
6. Diff the candidate symbol against the target symbol (§6.5) for each version in `TARGET_VERSIONS` where the TU has version conditionals, otherwise for the primary version only.
7. Persist every candidate and result to `.history/NNN.{c,json}` (the JSON includes per-version match, score, reasons, fired hint rule ids and wall time), and the counters in `.history/state.json`. Track `best_score`. Enforce `MAX_CHECKS` and `STAGNATION_LIMIT` (consecutive checks without a new best, rejected and failed compiles included): when either trips, print `STOP: <reason>. Write SUMMARY.json and end.` and exit with code 3. A `./check` after the limit prints `STOP` and does not compile.
8. Median wall time per check must be **≤ 3 s** on the worker host (Phase 1 exit criterion).

### 6.5 Diff and scoring

**Match (binary)**, all of the following for the symbol, computed from the ELF objects (`diff/match.py`), independent of any disassembler:
- identical symbol size
- identical code bytes with relocation sites zeroed (literal pool values included)
- identical relocation lists: (offset, kind, normalized target). Kind is `call:bl`/`call:blx` for calls, the relocation type otherwise. Normalized target:
  - named symbols: name and addend. The function's own name is `<self>` (so a renamed bench symbol still matches).
  - anonymous targets (string literals such as `@123`, section-relative references): the referenced bytes (strings shown as text, other data as length and hash, `.bss` as size). Isolation changes their offsets and numbering, not their contents.
  - function-scope statics (mwcc `name$NNNN`, the counter depends on the whole TU): name stem plus content.
  - a call the assembler resolved without a relocation (callee in the same section, common in assembly TUs) counts as a call to that callee.
Relocations are read as RELA; REL implicit addends are decoded from the patched bytes.

**Score (0–100)** for partial credit: align the normalized instruction sequences (Levenshtein). Cost per aligned pair: exact 0, same mnemonic with different operands 0.5, different mnemonic 1, insertion or deletion 1. `score = 100 × (1 − cost / max(len_target, len_candidate))`. A score of 100 without a binary match (for example a pool or relocation mismatch) displays as `99.9`.

**Backends:**
The backend only produces the rows for score, hints and rendering:
- `objdump` (default): `$OBJDUMP -d -r --no-show-raw-insn`, relying on the objects' ARM mapping symbols for the mode (TARGET.md records whether a target's objects carry them). Lines are normalized: offsets relative to the function, local branch targets as `.L<k>` (numbered by target order), calls and pool loads show the normalized target (`ldr r0, =Symbol`, `ldr r0, =0x1f4`).
- `objdiff`: `objdiff-cli diff -1 A -2 B -o - --format json` has a one-shot mode, but it emits a whole-object `DiffResult`. Adopt it only if objdump rows prove insufficient.

### 6.6 Text rendering and hints (what the worker sees)

```
check 7/40 | score 82.4 (best 85.1) | target 36 insns, yours 38 | version us
   12  add  r0, r4, r1          | add  r0, r1, r4          ~operands
   19                           | lsl  r0, r0, #0x18       +extra
   20                           | lsr  r0, r0, #0x18       +extra
   27  bhs  .L3                 | bge  .L3                 ~opcode
(29 matching lines collapsed)
hints:
 - extra lsl/lsr #24 pair → narrower type in your code than the original (u8 vs int?)
 - bhs vs bge → signedness of a comparison differs
```

On a binary match (§6.5) for every required version, the first line is exactly `MATCH` followed by the check counter, and nothing else is printed. Otherwise: show only non-matching lines plus 1 line of context, collapse equal runs, and cap at 60 lines. Hints are deterministic pattern rules (`diff/hints.py`), grouped into one rule set per ISA and selected by the function's mode. The first rule set (ARM/Thumb) is seeded with:

| Pattern | Hint |
|---|---|
| extra or missing `lsl #24`/`#16` followed by `lsr`/`asr` | type width mismatch (`u8`/`s8`/`u16`/`s16` vs `int`); `asr` means signed |
| `bhs/blo/bhi/bls` vs `bge/blt/bgt/ble` | signed vs unsigned comparison |
| same instructions, permuted registers | declaration order, temporaries, or expression operand order |
| `push`/`pop` register sets differ | number of simultaneously live variables |
| target has a jump table, candidate doesn't (or vice versa) | `switch` vs `if` chain, or case density |
| missing `bl X` with inlined body in its place (or vice versa) | inline function or macro vs call |
| literal pool value differs | wrong constant or enum |

Hints are suggestions and say so. New hints are added only with a test fixture.

### 6.7 Similarity and examples

- Feature vector per function: mnemonic 3-grams (MinHash, 128 permutations), callee set, pool constant set, instruction count bucket.
- `sim(a,b) = 0.6·jaccard(ngrams) + 0.3·jaccard(callees) + 0.1·jaccard(constants)`.
- Example pool = matched functions (human C at pin + rematch matches with lint pass). Bench mode excludes the target and, by default, all functions from the target's own TU (live neighbors are usually assembly, so including them would inflate the bench).
- Each example goes in the sandbox as `examples/NN_<name>.c` + `examples/NN_<name>.s`.
- Phase 3: cross-repo pool from `REFERENCE_REPOS`. Each reference repo is built once with its own toolchain, and objects are disassembled into the same feature space. Reported as a separate ablation.

---

## 7. Worker

### 7.1 Sandbox layout (one per attempt)

Claude Code loads `CLAUDE.md` / `AGENTS.md` from the working directory **and every directory above it** (unless `--bare`). A sandbox inside the harness repo would therefore inject this repo's agent instructions into every worker. Sandboxes live under `REMATCH_WORKDIR`, outside all repos, and `rematch doctor` fails if `REMATCH_WORKDIR` or any ancestor contains `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md` or `.claude/`, except Claude Code's user config dir (§4).

```
$REMATCH_WORKDIR/attempts/<attempt_id>/
  TASK.md          # rendered from prompts/task.md.j2 (Appendix B)
  target.s         # the target function's assembly + its literal pool (live: from the asm TU; bench: disassembly of vanilla object)
  ctx.h            # §6.3
  examples/        # §6.7
  draft.c          # optional m2c draft (Phase 3)
  LEARNINGS.md     # copy of docs/LEARNINGS.md
  candidate.c      # starts as draft.c or a stub with the right signature if known
  check            # executable wrapper → rematch cd (§6.4)
  task.json        # symbol, target symbol, TU, mode, per-version compile argv and context, limits
  ctx.<version>.h  # context for each extra version of a version-conditional TU
  .target/         # normalized vanilla lines and match view per version (written by the builder)
  NOTES.md         # worker may append proposed learnings here
  SUMMARY.json     # worker writes at the end (untrusted)
  .history/        # written only by ./check
```

Before launch: compute SHA-256 of every file except `candidate.c`, `NOTES.md` and `SUMMARY.json`, and `chmod 0444` them. After the attempt: re-hash. Any change means outcome `invalid`.

### 7.2 Bench presentation rule

The target function's own name is replaced with `anonymous_name(module, address)` (typically `sub_<ADDR>`) in `target.s`, `TASK.md` and the expected symbol. Callee names stay as they are in the pin. **Known realism gap (v1):** callees may have human-chosen names that weren't available when the function was decompiled. A "historical presentation" mode that uses names from the cutoff-commit snapshot and maps them through addresses is a Phase 3+ improvement.

### 7.3 Invocation (verified against Claude Code docs, Nov 2026)

Run from the sandbox directory (cwd = sandbox).

**Subscription mode (default):**
```bash
claude -p "$(cat TASK.md)" \
  --model "$MODEL" --effort "$EFFORT" \
  --append-system-prompt-file "$REMATCH/prompts/worker_system.md" \
  --tools "Read,Edit,Write,Bash" \
  --allowedTools "Read" "Edit" "Write" "Bash(./check)" \
  --permission-mode dontAsk --permission-prompts none \
  --strict-mcp-config --disable-slash-commands \
  --max-turns "$MAX_TURNS" \
  --no-session-persistence \
  --output-format stream-json --verbose
```

**API mode:** the same command plus `--bare` and `--max-budget-usd "$MAX_BUDGET_USD_PER_ATTEMPT"`, with `ANTHROPIC_API_KEY` set. Bare mode doesn't use subscription login.

Notes:
- In `dontAsk`, anything not allowed is denied without prompting, and `--permission-prompts none` tells Claude not to retry denied calls. Requires Claude Code ≥ 2.1.259.
- **VERIFY (Phase 2):** in subscription mode, inspect the `system/init` event and the first turn for leaked user-level configuration (`~/.claude` CLAUDE.md, hooks, plugins). If anything leaks, run workers with a dedicated Claude config directory, or switch to API mode.
- Timeout: send SIGINT at `ATTEMPT_TIMEOUT_SEC`, which ends the turn cleanly, then SIGTERM after 30 s (exit 143).
- Parse the final `result` event for `num_turns`, usage tokens and `total_cost_usd`. Cost is a client-side estimate.
- Save the full stream to `var/logs/<run_id>/<attempt_id>.jsonl`.

### 7.4 Outcome classification

After exit: re-hash the sandbox (`invalid` if anything changed). Then re-run `rematch cd` on the **best** `.history` candidate (never on the worker's word):

- 100% match on all required versions and lint pass → `match`, insert into `matches`, function status becomes `matched`
- 100% match but lint fail → `nonmatch` (lint failure noted). It stays retryable with the lint errors shown next time.
- Otherwise the most specific of: `compile_fail` (never compiled), `stagnation`, `check_limit`, `turn_limit`, `timeout`, `budget`, `nonmatch`, `error`
- Parse `NOTES.md` bullets into `learning_candidates`.

---

## 8. Orchestrator

### 8.1 Function states

```
untried → leased → matched → integrated
             ↘ failed → (cooldown) → leased (escalation+1)
                    ↘ stuck  (attempts ≥ MAX_ATTEMPTS_PER_FUNCTION and ladder exhausted)
any → superseded   (upstream humans decompiled it, §12)
any → skipped      (excluded by policy)
```

### 8.2 Leasing

Leases use `lease_owner` + `lease_expires` (attempt timeout + 5 min), with a heartbeat every 30 s. On startup, expired leases revert to their previous state. At most one active attempt per function.

### 8.3 Difficulty (initial heuristic, configurable)

```
difficulty = n_insns + 8·n_branches + 25·has_switch + 2·pool_entries
           + 10·unknown_callees − 40·max_sim_to_matched
```

`unknown_callees` = callees whose prototype is not in the header index. Pick order: `untried` by ascending difficulty, then `failed` whose cooldown has expired, by ascending difficulty. Recompute `max_sim` after every 50 new matches.

### 8.4 Escalation ladder

Each failed attempt moves to the next rung of `ESCALATION_LADDER` (model:effort). Rung-specific changes:
- rung ≥ 1: include the previous best candidate and its diff in the sandbox as `previous_best.c` / `previous_best.diff`
- rung ≥ 2: TU-context compile mode if available
- rung ≥ 3: double `EXAMPLES_K`, and include an m2c draft if not already present

After the last rung, the function becomes `stuck` and goes into the human review queue (`rematch stuck --list`).

---

## 9. Readability lints (acceptance gate)

A match is accepted only if `lint/readability.py` passes:
- no inline asm, no `#pragma`, no `#include` other than `ctx.h`
- no `goto` unless the target's control flow requires it (allowed only on rung ≥ 3, flagged for review)
- no `do { … } while (0)` outside macros; no gratuitous `volatile` (any `volatile` gets flagged for review)
- no unused locals, no self-assignments, no no-op casts chains (`(int)(u32)(int)x`)
- identifiers: unknown struct fields as `unk_XX` (hex offset), unknown locals as descriptive or `var0..`; never invent "meaningful" names for things not evidenced (callee names, string contents, or constants are evidence)
- clang-format clean using the target repo's `.clang-format`, if it has one

Lint results are stored in `matches.lint_json`.

---

## 10. TU integration (`rematch integrate`)

Runs only in the live worktree on branch `rematch/live`, one TU at a time, serialized behind a file lock.

1. Preconditions: every function in the assembly TU is `matched`, and the TU's data sections are converted (a `tu_data` task produces C definitions for its data, rodata and bss sections, verified by the whole-object diff in step 3).
2. Build the TU's C file: includes (union of needed headers, minimal), data definitions, then functions **in address order**.
3. Compile the TU and diff **all sections** of the new object against the vanilla assembly TU's object, for every version. On mismatch, spawn a `tu_fix` attempt with the object-level diff. Typical causes: data or string ordering, `static` vs external linkage, function order.
4. `swap_tu_to_c`: replace the assembly TU with the C TU in the target's link-order source file (never in generated files), and `git rm` the assembly file.
5. Build **all** versions from clean, and check every hash.
6. On pass: commit `rematch: decompile <asm TU> (N functions)`, set `integrated` and `integrated_commit`. On fail: `git restore`, mark the TU `integration_failed` in the run log, and leave the functions as `matched`.
7. Push only to the target's private `origin`. The target's pre-push hook (§2.3) rejects any URL in `forbidden_push_urls`.

---

## 11. Benchmark (`rematch bench`)

### 11.1 Pool

- `decompiled_at` for each `c_human` function = the first first-parent commit where its `(module, address)` disappears from assembly. Computed by `inventory/history.py`, which walks snapshots at roughly weekly granularity, then bisects.
- Clean pool = `c_human` functions with `decompiled_at > BENCH_CUTOFF_DATE`.
- "Possibly contaminated" pool = everything else, reported separately and never mixed.

### 11.2 Sets

`rematch bench --n N --seed S [--pool clean|contaminated]` creates or reuses a frozen set: stratified by size bucket (S: <40 insns, M: 40–150, L: >150) proportional to the pool. Set id `b<N>-s<S>-c<cutoff>`. Prompt and context changes **must** be compared on the same set id.

### 11.3 Metrics (`rematch report`)

Per run, overall and per size bucket: match rate; first-check compile rate; median checks to match; median turns; mean input, output and cache tokens per attempt and per match; cost per match (estimate); wall time per attempt; outcome distribution; lint-failure rate; escalation rung at match. Append a summary row to `docs/benchmarks.md` (format in CONTRIBUTING.md).

---

## 12. Upstream sync (`rematch sync-upstream`)

Fetch `upstream`, merge into the target's `main` (never push upstream), then rebase `rematch/live`. Policy when a human decompiled a TU we also integrated: **take upstream's version**, drop our commit for that TU, and mark our rows `superseded`. Re-run inventory, then re-pin (`TARGET_PIN`) and rebuild vanilla only between runs.

---

## 13. CLI

```
rematch doctor                      # Claude Code version, workdir, push guard, vanilla builds, hashes, DB
rematch inventory [--history]       # parse target; --history computes decompiled_at
rematch selftest                    # oracle + saboteur agents, zero tokens (§14)
rematch cd --sandbox DIR [--json-out F] [--mode isolated|tu]   # used by ./check
rematch ctx FUNCTION --out DIR [--with-source]   # build a sandbox without a worker; --with-source seeds human C (never for bench)
rematch roundtrip [--n N --seed S | --golden]    # human C through ./check must match (Phase 1 exit; .rematch/golden.toml)
rematch attempt FUNCTION [--model M --effort E]                # one live attempt
rematch bench --n N --seed S [--pool clean] [--workers W] [--model M --effort E]
rematch run --workers W [--max-attempts K] [--until HH:MM]     # live loop
rematch integrate [--tu TU | --ready | --verify-branch]        # --verify-branch: rebuild every rematch/live commit, check hashes
rematch report [--run ID | --last] [--compare ID]
rematch stuck --list | --show FUNCTION
rematch learnings review            # interactive accept/reject of candidates
rematch sync-upstream
```

`rematch doctor` prints one `PASS`/`FAIL` line per check and exits 1 if any check fails. Checks, in order: `CLAUDE_BIN --version` ≥ 2.1.259 (spends no tokens); `REMATCH_WORKDIR` and ancestors clean (§4); `target.toml` valid (§2.1); no push URL in `TARGET_REPO` matches `forbidden_push_urls` and the installed pre-push hook matches `.rematch/pre-push` (§2.3); vanilla worktree at the pin (§6.2); `TOOLCHAIN_EXEC` runs `true` (if set); the toolchain sees `REMATCH_WORKDIR` (sandboxes are compiled through it); per-version build and hash (§6.2); `required_paths` present; `OBJDUMP` runs; `CTAGS` is Universal Ctags; DB migrated (§5).

Model and effort: `attempt` and `bench` use `--model`/`--effort` if given, else `WORKER_MODEL`/`WORKER_EFFORT`. `run` starts each function at rung 0 of `ESCALATION_LADDER` and escalates through it (§8.4). The resolved model and effort are stored on every attempt.

Every command that can spend tokens (`attempt`, `bench`, `run`) prints an estimate of attempts and requires `--yes`, or an interactive confirmation when attached to a TTY.

---

## 14. Testing requirements

- Unit: asm parser (fixtures with each ISA mode, pools, switch tables, conditional blocks), C parser, header index, difficulty, similarity, lints, hint rules (one fixture per rule), score function (golden alignments), target.toml validation, doctor checks.
- `rematch cd` golden tests: 20 known human-C functions compiled from their original C must report `match: true`. 5 deliberately mutated variants must report the expected diff categories.
- **Oracle agent** (`selftest/oracle_agent.py`): a fake `claude` that copies the human C into `candidate.c` and runs `./check`. A full bench run on 10 functions must yield 10 matches, end to end through the orchestrator and DB, with zero model calls.
- **Saboteur agent:** tries to edit `ctx.h` or `target.s` and write outside the sandbox. It must produce outcome `invalid`, and the vanilla worktree must be byte-identical afterwards.
- CI-equivalent local command: `uv run pytest -q && uv run ruff check && uv run mypy src`.
- Tests never invoke the real `claude` binary. `CLAUDE_BIN` is swapped for the fakes.

---

## 15. Security and hygiene

- Workers can only touch their sandbox. The vanilla worktree is read-only to workers (enforced by `chmod` on the vanilla tree after the build, plus sandbox hashing).
- The integrator is the only writer to the live worktree.
- No network tools for workers (`--tools` excludes WebFetch and WebSearch; `--strict-mcp-config` with no config).
- Secrets only in `.env`. Never log `ANTHROPIC_API_KEY`.

---

## Appendix A: `prompts/worker_system.md` (draft, prompt_version: 1)

`{{ target_intro }}` is filled from the target (a `target.toml` field added in Phase 2), for example the platform, compiler and default ISA mode.

```markdown
prompt_version: 1

You are doing matching decompilation. {{ target_intro }} Your job: write C in
candidate.c that compiles to EXACTLY the instructions in target.s.

Rules:
- Edit ONLY candidate.c (and optionally append to NOTES.md). Every other file
  is read-only; changing them invalidates the attempt.
- After every meaningful edit, run ./check. It is the only judge. You are done
  only when it prints "MATCH". Never claim a match yourself.
- No inline asm, no #pragma, no #include other than "ctx.h". No tricks whose
  only purpose is to fool the compiler (pointless volatile, goto, do/while(0),
  redundant casts). The code must read like plausible original source.
- Name unknown struct fields unk_XX by hex offset. Do not invent meaningful
  names without evidence (callee names, strings, constants).
- Use ctx.h for types and prototypes; do not redefine what it declares.
- Read examples/ first: they are similar functions that already match. Copy
  their idioms.
- Read LEARNINGS.md: compiler quirks discovered so far.
- If ./check says STOP, write SUMMARY.json and end immediately.
- Don't flail. If the score hasn't improved in a few checks, step back, re-read
  target.s, and change the structure (loop form, if/else vs ternary, variable
  types, declaration order) rather than shuffling tokens.

When you finish (match or STOP), write SUMMARY.json:
{"matched": bool, "best_score": number, "what_worked": string,
 "open_issues": string}
and, if you learned something general about this compiler, append one-line
bullets to NOTES.md (e.g. "- u8 locals compared to constants emit lsl/lsr; use int").
```

## Appendix B: `prompts/task.md.j2` (draft)

```markdown
# Task: match {{ symbol }} ({{ mode }}, {{ n_insns }} instructions, module {{ module }})

Files:
- target.s: the original assembly for {{ symbol }} (and its literal pool)
- ctx.h: types, prototypes, constants
- examples/: {{ examples|length }} similar functions with matching C
{% if draft %}- draft.c: an automatic first draft (often wrong in details){% endif %}
{% if previous_best %}- previous_best.c / previous_best.diff: best earlier attempt ({{ previous_best_score }}%){% endif %}

Budget: at most {{ max_checks }} runs of ./check. Stop early if you match.
{% if version_cond %}This function is checked against every target version.{% endif %}

Write the function {{ symbol }} in candidate.c, run ./check, iterate to MATCH.
```
