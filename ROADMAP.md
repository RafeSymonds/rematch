# ROADMAP

Each phase ends with **exit criteria** that can be checked mechanically. Don't start a phase until the previous one's exit criteria pass. Phases are generic. Per-target progress and evidence (command output, report IDs) are recorded in that target's `.rematch/TARGET.md` and `verify-log.md`.

Token spend starts in Phase 2.

---

## Phase 0: Harness skeleton and first target ready

- Target repo has `.rematch/` (`target.toml`, `TARGET.md`; SPEC §2).
- Vanilla worktree at `TARGET_PIN` builds every version on the build host.
- Resolve the target's Phase 0 **VERIFY** items in TARGET.md: object paths for asm and C TUs, map file location.
- Harness skeleton: `pyproject.toml`, Typer CLI, config loading, DB schema and migrations, `rematch doctor`.
- Type-recovery schema (SPEC §5, §16): `shared_types`, `provisional_types`, `type_uses`, `name_proposals`, and the `type_merge` and `naming` task kinds. The tables stay empty until Phase 2.

**Exit criteria**
- [ ] `rematch doctor` passes: Claude Code ≥ 2.1.259, `REMATCH_WORKDIR` clean (SPEC §4), every vanilla ROM hash matches `target.toml`, `required_paths` present, DB migrated to the latest schema (type-recovery tables included)
- [ ] Your own dump's hash equals the target's expected hash for that version (optional, recommended)
- [ ] The target's TARGET.md has no remaining Phase 0 VERIFY markers

---

## Phase 1: Inventory and `rematch cd`

- Asm and C parsers, module mapping from the link-order file, header index, difficulty.
- Toolchain wrapper: exact compile argv from the vanilla build, isolated mode.
- Diff backend: verify objdiff (SPEC §6.5 VERIFY), otherwise implement the objdump backend. Score, hints and text rendering.
- `./check` wrapper with history, `MAX_CHECKS` and `STAGNATION_LIMIT`.
- Decide where target code hooks live (more `target.toml` sections or `.rematch/adapter.py`, SPEC §6.0).

**Exit criteria**
- [ ] `rematch inventory` counts are within ±2% of the snapshot in TARGET.md, and the totals are explained in the run log
- [ ] Golden test: 20 human-C functions compiled from their original C report `match: true`, and 5 mutated variants report the expected diff categories
- [ ] On 50 random human-C functions, at least 49 match from their own C in isolated mode. Every miss is explained and documented (expected cause: file-level IPA / inlining context).
- [ ] Median `./check` wall time ≤ 3 s on the worker host
- [ ] `uv run pytest -q` green

---

## Phase 2: Single worker and Benchmark v0

- Sandbox builder (SPEC §7.1), read-only inputs plus hashing, bench presentation rule (§7.2).
- Worker invocation (§7.3) with both auth modes, timeout handling, stream capture, outcome classification (§7.4).
- `inventory --history` computes `decompiled_at`. Clean pool, frozen bench sets.
- Oracle and saboteur self-tests.
- `rematch report`.
- Provisional-types convention (SPEC §16.1): prompt rule, pre-filter and lint for the `/* rematch:types */` block, the offset/size probe, and `provisional_types` rows stored with each match.
- Resolve SPEC §7.3 VERIFY (config leakage in subscription mode).

**Exit criteria**
- [ ] `rematch selftest` passes: oracle 10/10 matches, saboteur produces `invalid`, vanilla tree unchanged, zero model calls
- [ ] `rematch bench --n 100 --seed 42 --pool clean` runs unattended to completion
- [ ] Baseline row recorded in the target's `.rematch/benchmarks.md`: match rate, median checks, tokens and cost per match, wall time, all per size bucket
- [ ] A match whose candidate declares provisional types stores them with compiler-measured offsets and sizes (selftest fixture)
- [ ] No target number is required. The point is an honest baseline.

---

## Phase 3: Context and retrieval (iterative)

Changes are made one at a time, each measured on the **same** frozen set (`b100-s42-c20260701`) against the baseline:
- similarity-ranked examples (in-repo)
- curated learnings: `learnings review` (human) and `learnings curate` (agent with evidence that rematch recompiles), compiler and target scopes, `learnings verify` before every run
- m2c draft seeding (`draft.c`)
- cross-repo examples from `REFERENCE_REPOS` (related decomp repos; build them once; opcode-feature index)
- prompt iterations (bump `prompt_version` every time)
- optional: historical presentation mode (SPEC §7.2)
- optional: types-stripped bench variant (SPEC §11.2, §16.4), which measures layout recovery and is reported as `-ts` sets, separately from the plain bench

**Exit criteria**
- [ ] On the frozen set, either match rate improves by ≥ 10 percentage points over baseline, or cost per match drops by ≥ 30% at an equal or better match rate
- [ ] Ablation table in the target's `.rematch/benchmarks.md` showing each change's isolated effect
- [ ] A second frozen set (`b100-s7-…`) confirms the gain isn't overfit to set 42 (within ±5 points)

---

## Phase 4a: Land live matches

The live pilot (50 untried functions) matched 96% at $0.10 per match once two checker artifacts were fixed, so landing matches became the bottleneck. Whole-TU integration rarely completes: the median assembly TU has 21 functions. So integration is run-based (SPEC §10).

- `rematch recheck`: re-verify misses after checker fixes (no tokens).
- Readability lints as the integration gate (§9).
- Run-based integrator on `rematch/live`: split assembly TUs, C pieces checked before writing, every version built with every hash checked, commit or restore.
- `tu_fix` task kind for pieces whose candidates conflict.
- `rematch run`: leases with heartbeat and dead-process recovery, TU-ordered work, escalation ladder with `previous_best.c`, background integration, graceful shutdown.
- `rematch status`.
- `rematch publish`: merge `rematch/live` into a chosen branch, verified, and push it. `run` publishes when it ends.

**Exit criteria**
- [x] At least 1 assembly TU fully converted, integrated and committed on `rematch/live`, with every version's hash passing on that commit
- [x] A 2-hour unattended `rematch run`, killed and restarted, with no orphaned leases (2h run: 1,784 attempts, 94% matched, 0 errors, 0 leases left; kill and restart recovered every lease in the shakedown)
- [x] `integrate --verify-branch` passes on every `rematch/live` commit (22 of 22 at the time of the check)
- [x] Report shows the live match rate, cost per match and the outcome distribution

## Phase 4b: Types and data for integrated code

- Integration batching: one build per batch of TU steps, culprit by name or bisection, crash journal, plan-hash skip, parallel preparation (SPEC §10).
- Harness fixes from the 2h run: label-named assembly targets (jump tables), ARM-mode wrapping, block placement, rung per attempt.
- `type_merge` (SPEC §16.2). Module-wide clustering was measured first (`rematch types merge`, a dry run: 16% of provisional types had an evidence-backed partner) and replaced by a per-TU agent step (`integrate --types`) gated by the piece check.
- Reference layouts from `REFERENCE_REPOS` (SPEC §16.3): deferred until a reference repo is set up.
- Lint: pointer math on struct pointers is flagged (SPEC §9, `LINT_VERSION` 2).
- `tu_data`: move a fully converted TU's data sections into C so it leaves no assembly (`integrate --data`, data check by location).
- `rematch run --finish`: the types and data steps run with the live loop, in one prepare pool with the function step (new functions first).
- 24-hour unattended live run.

**Exit criteria**
- [x] At least one TU whose duplicate provisional types a `type_merge` attempt merged, every function re-verified by the piece check, and at least one failed attempt that left the piece unchanged (TU-local merging replaced module-wide clustering, SPEC §16.2)
- [x] An integrated TU compiles against shared or TU-local types only (no provisional duplicates)
- [x] A fully matched TU with data integrated with no assembly left
- [ ] 24-hour unattended live run with no crashed orchestrator and no orphaned leases after restart

---

## Phase 5: Long tail (ongoing)

- [x] Stagnation diagnostics: `rematch stuck` tallies failed functions by the hint categories of their best check.
- TU-context compile for asm targets via a GLOBAL_ASM tool such as asm_processor.
- Escalation ladder tuning: `rematch report` shows each rung's match rate and cost per match. Readability cleanup pass on accepted matches (must stay 100%).
- [x] Human review queue (`rematch stuck`, `--show`, `--accept`). Human fixes feed back as learnings candidates.
- Naming pass (SPEC §16.5): a cheaper model proposes function, type and field names from evidence, a human approves them with `rematch names review`, and the integrator applies accepted renames on `rematch/live` with the usual hash check.
- Upstream sync routine (§12).
- [x] Prototype overrides (SPEC §6.4, §10): a `rematch:protos` block corrects a placeholder header prototype; integration edits the header and later work reads rematch/live's committed headers. A quarter of the first real run's failures were blocked by such prototypes.
- [x] Context headers are preceded by the headers defining the types they use (context v11), for headers that only compile after others.

**Exit criteria (rolling, weekly)**
- [ ] Weekly report: new matches, integrated TUs, stuck-queue size and trend, cost per match, shared types accepted, names approved
- [ ] Stuck queue shrinks or holds steady for 3 consecutive weeks, or a written decision to stop

---

## Phase 6: Second target (optional)

- Pick a second target. For a game with no decomp project yet, first bootstrap a byte-identical rebuild at 0% decompiled (for DS games, `ds-decomp`: `dsd rom extract` → `init` → `delink` → `lcf` → link → `rom build` → `check modules`).
- Write its `.rematch/`. If it has no human C, it has no bench pool. Reuse prompts and learnings where the compiler matches.

**Exit criteria**
- [ ] `rematch doctor` passes on the new target
- [ ] `rematch run` produces verified matches on the new target
