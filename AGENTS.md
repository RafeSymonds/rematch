# AGENTS.md

Instructions for any coding agent working on **rematch**, a harness that drives headless Claude Code workers to produce byte-matching C for matching-decompilation projects. Each project is a target that describes itself in `<TARGET_REPO>/.rematch/` (SPEC §2).

Read in this order before changing anything: `SPEC.md` (authoritative contracts) → `ROADMAP.md` (what phase we're in) → `DESIGN.md` (why) → the target's `.rematch/TARGET.md` (target facts). Work only on the current phase unless told otherwise.

## Hard rules

1. **Never push, PR or open issues against a target's upstream.** Many decomp projects ban LLM-generated contributions. Each target lists its forbidden URLs in `target.toml` (`forbidden_push_urls`), its upstream push URL is disabled, and its pre-push hook blocks them. Don't "fix" that.
2. **Never commit ROMs, BIOS files, firmware or save files**, or anything extracted from a cart that isn't already in the target repo. `.gitignore` covers these, so keep it that way.
3. **Never write to the target repo** except through `rematch integrate` on branch `rematch/live` in the live worktree, or edits to its `.rematch/` directory. The vanilla worktree is read-only, always.
4. **Never run anything that spends model tokens** (`rematch attempt|bench|run`, or the real `claude` binary) unless the maintainer explicitly asked in this session. Tests and selftests use the fake agents in `selftest/`.
5. **SPEC is the contract.** If you change a contract (schema, CLI, tool output, sandbox layout, invocation flags, target.toml fields), update SPEC.md in the same change. Resolve VERIFY items with evidence and record the evidence.
6. **Don't weaken the checker.** Nothing may make `./check` or outcome classification report a match the binary comparison in SPEC §6.5 wouldn't.
7. **Keep this repo target-agnostic.** No game names, target paths, host names, machine details or personal setup. Target facts go in the target's `.rematch/`. Machine settings go in the gitignored `.env`.

## Commands

```bash
uv sync                                  # install
uv run pytest -q                         # tests (must stay green)
uv run ruff check && uv run ruff format --check
uv run mypy src
uv run rematch doctor                    # environment, push guard, vanilla builds, hashes
uv run rematch inventory                 # safe, no tokens
uv run rematch selftest                  # oracle/saboteur agents, no tokens
uv run rematch ctx <FUNCTION> --out /tmp/sbx   # build a sandbox to inspect, no tokens
```

The target's build commands are in its `target.toml`. Never build inside the vanilla worktree after `rematch doctor` pins it.

## Code style

- Python 3.12, type-annotated, `mypy --strict` on `src/`. Ruff for lint and format.
- Small modules matching the SPEC §3 layout. Pure functions for parsing, scoring and hints so they're easy to test.
- Subprocesses always get explicit timeouts and captured stderr. Never use `shell=True` with interpolated paths. Target toolchain commands go through `toolchain/runner.py` so `TOOLCHAIN_EXEC` applies.
- Every new hint rule, lint rule or parser case comes with a fixture test.
- Log structured JSON per attempt. Never log secrets.

## Definition of done (any task)

- Tests added or updated and passing. Lint and type checks are clean.
- SPEC/ROADMAP updated if contracts or phase status changed.
- For changes affecting worker behavior (prompts, context, sandbox, hints): note the `prompt_version` / `context_version` bump, and say that a benchmark comparison is needed. Don't run it yourself unless asked (rule 4).

---

## Traps: building this harness

These are mistakes LLM coding agents reliably make on this kind of project. Check your work against each one.

1. **Letting workers see the repo.** Running `claude -p` from the target or harness root makes the worker explore, burn tokens and "fix" headers. Workers run in a sandbox containing only SPEC §7.1 files. The sandbox must also sit **outside every repo**: Claude Code loads `CLAUDE.md`/`AGENTS.md` from the cwd and all its ancestors, so a sandbox under `rematch/var/` would feed this file to every worker. Use `REMATCH_WORKDIR` (SPEC §4).
2. **Trusting the worker's word.** A worker saying "MATCH" or writing `"matched": true` in SUMMARY.json is not evidence. The orchestrator re-runs `rematch cd` on the best history candidate. SUMMARY.json is untrusted.
3. **Checking the wrong object.** Comparing against a freshly built object, or against the live worktree, silently compares the candidate to itself. Targets always come from the pinned vanilla worktree.
4. **Guessing build paths and flags.** Object paths and compiler argv come from the vanilla build's `build.ninja` / `compile_commands.json`, not from string templates. A flag mismatch (a missing version define, the wrong CPU flag, a missing IPA flag) makes matches impossible or fake.
5. **Ignoring the other versions.** TUs with version conditionals must match for every version. Integration builds every version.
6. **Diffing text instead of objects.** Comparing objdump text without normalizing relocations, addresses and literal pools produces false matches and false mismatches. Match means bytes, relocations, pool and size (SPEC §6.5).
7. **Mode decode errors.** Some compilers emit objects without mapping symbols (ARM `$t`/`$a`), so objdump decodes in the wrong mode and the diff turns to garbage. Set the mode per symbol from inventory.
8. **Benchmark contamination.** Using old public C as the benchmark measures memorization. Use the post-cutoff clean pool and report contaminated results separately. Don't leak the target's human name (SPEC §7.2) or same-TU neighbors into bench examples.
9. **Comparing runs on different sets.** A prompt "improvement" measured on a different random sample means nothing. Use frozen set IDs.
10. **Accidental token spend in tests.** Any test path that can reach the real `claude` binary is a bug. Tests set `CLAUDE_BIN` to a fake and assert it.
11. **Over-broad permissions.** `--allowedTools Bash` or `bypassPermissions` lets a worker run arbitrary commands. Use the exact SPEC §7.3 flags: `dontAsk`, `--permission-prompts none`, `Bash(./check)` only. Don't depend on allow rules alone: read-only inputs plus hashing are the backstop.
12. **Killing workers mid-turn with SIGTERM first.** SIGTERM leaves the turn unrecorded (exit 143). Send SIGINT first, then SIGTERM after a grace period.
13. **Append-only learnings.** Dumping every worker note into LEARNINGS.md bloats every prompt and spreads wrong "rules". Notes go to `learning_candidates` and get curated.
14. **Integrating per function.** Assembly files are whole TUs in link order. Integrate whole TUs (SPEC §10), and only after a whole-object diff passes.
15. **Editing generated files.** Many targets generate their linker script and build lists from one link-order source file (TARGET.md names it). Edit only that source.
16. **Non-idempotent inventory.** Inventory keyed by name breaks when upstream renames `sub_XXXXXXXX` to a real name. Key by `(module, address)`.
17. **Leaking target or machine details into this repo.** Hardcoded target paths, game names in code, or host-specific commands belong in the target's `.rematch/` or in `.env`.

## Traps: what decompilation workers get wrong (the harness must defend against these)

These go into prompts, lints and checker rules. Don't remove a defense without a benchmark showing it's unnecessary.

1. **"Functionally equivalent" is not a match.** Models stop at "this does the same thing". Only `./check` printing `MATCH` counts.
2. **Gaming the compiler.** Inline asm, `#pragma`, gratuitous `volatile`, `goto`, `do{}while(0)` or chained no-op casts can force a match while producing unreadable, unmaintainable source. These are blocked by checker pre-filters and lints (SPEC §6.4, §9).
3. **Permuter-style noise and doom loops.** Random token shuffling ping-pongs between two variants, and Claude treats noise as signal. Defenses: `STAGNATION_LIMIT`, a prompt instruction to change structure rather than shuffle, and no permuter output in context.
4. **Editing shared context to force one function.** Changing a struct layout or prototype in a header to fix one function breaks many others. `ctx.h` is read-only. Header fixes are a human or integrator decision.
5. **Hallucinated semantics.** Inventing field names from vibes, or wrong struct offsets. Unknown fields are `unk_XX` by offset, and names need evidence.
6. **Wrong type widths and signedness.** On ARM, `u8`/`u16`/`s16` vs `int` adds `lsl`/`lsr`/`asr` pairs, and signed vs unsigned changes `bge` ↔ `bhs`. These are the most common near misses, and hints target them.
7. **Register allocation sensitivity.** Declaration order, temporaries and expression operand order change register assignment. Workers try to change opcodes when only the order is wrong.
8. **Control-flow shape.** `if/else` vs ternary, `for` vs `while`, early return vs single exit, and `switch` density (jump table or not) all change code layout.
9. **Inlining and IPA context.** With file-level IPA or inlining, code can depend on neighboring functions in the same TU. A function stuck at 95–99% in isolation may need TU-context mode, not more token shuffling.
10. **Literal pools and data references.** Constants loaded from the function's literal pool show a wrong constant, enum or symbol there, not in the instructions. String and rodata ordering matters at TU integration time.
11. **GCC/Clang habits.** Old vendor compilers (for example Metrowerks CodeWarrior) aren't GCC. Builtins, attributes and statement expressions may not exist or may codegen differently. Copy idioms from `examples/`, not from general knowledge.
12. **Rewriting what's already right.** After a big structural change drops the score, models abandon the best version. `./check` keeps history, and the next escalation rung gets `previous_best.c`.
