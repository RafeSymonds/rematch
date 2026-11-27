# DESIGN: why rematch is shaped this way

SPEC.md says *what* to build. This document says *why*. If they ever disagree, SPEC.md wins and this file gets updated.

## 1. Goal

Build an agent system that converts compiled assembly into C which, compiled with the original toolchain, reproduces the original bytes exactly. Measure it honestly. Then use it on:

1. **Existing matching-decomp projects (your own fork):** live progress on the functions still in assembly, plus a benchmark built from the functions humans already matched.
2. **Untouched games** (later), bootstrapped with a delinking toolkit such as `ds-decomp`.

### Non-goals

- Contributing to upstream decomp projects. Many ban LLM-generated contributions, and rematch respects that.
- A general-purpose decompiler, a PC port, or ROM hacking tooling.
- "Functionally equivalent" C. Only byte-identical counts.

## 2. Why this problem suits agents

Matching decompilation has a **perfect, instant, unfakeable checker**: compile, diff, and the answer is either 100% or not. Each function is a small, self-contained unit with a clear end state. Iteration is cheap (about a second per compile). The hard part is knowing which C shape produces which instruction sequence under a specific old compiler, which is exactly the pattern-matching-plus-iteration loop LLMs are good at, as long as the harness keeps them honest and focused.

## 3. Prior art and the lessons we're adopting

- **Chris Lewis, Snowboard Kids 2 / Snowboard Kids (N64), 2025–2026.** He ran Claude headless in a loop, feeding it "the cheapest function left". It was very effective early on (25% → 58%), then **progress slowed dramatically** in the long tail. Lessons:
  - Prioritize functions that have **similar already-matched functions**. Those references guide the model.
  - **Permuters were counterproductive.** Claude treated permuter noise as signal, which led to doom loops and token burn.
  - **Clean, idiomatic matched code is better future context** than brittle, ugly matches.
  - A shared **learnings file** of compiler quirks created a feedback loop between agents.
  - **Parallel git worktrees** gave several agents independent copies.
  - Exposing an **explicit deadline** to the agent reportedly improved output quality.
  - Running agents from the repo root **burned tokens on exploration**.
  - Snowboard Kids reached 100% in 84 days. That took community expertise as well as AI.
- **Mizuchi** (macabeus): a plugin pipeline that runs m2c, then Claude, then a permuter. It matched about 74% of a 60-function benchmark. Its Klonoa (GBA) decompilation reached 51% from a raw ROM. Lesson: **benchmark first**, and seed with a programmatic draft.
- **ds-decomp (`dsd`)**: the Rust DS decompilation toolkit, with objdiff and decomp.me integration. A path to targets that have no decomp project yet.

## 4. Key decisions

### D1. The harness is a separate repo from every target
The target repo stays a clean tree of its upstream project. rematch writes to it only through the **integrator**, on the `rematch/live` branch, plus the target's own `.rematch/` definition. The harness can be developed, tested and reset without touching any target.

### D2. Python 3.12 + SQLite
The hot path is the target's compiler and the model, not our code. Python gives the fastest iteration on parsing, prompts and orchestration. SQLite gives durable state, crash recovery and easy ad-hoc analysis. Rust remains an option for a hot component later (for example a custom differ) if profiling demands it.

### D3. Workers are `claude -p` sessions, one per attempt
This reuses Claude Code's agent loop, file tools and permission system, and works with a subscription login as well as an API key. Every attempt runs in a **fresh scratch directory** containing only what that function needs. One attempt is one session, with no carry-over except curated learnings. The Agent SDK is a later option if we need finer control. The CLI is enough to start.

### D4. Verify per function, integrate per TU
In most matching-decomp projects, assembly files are whole translation units, linked in the order given by a link-order file. A TU is either all assembly or all C. Mixing is possible with GLOBAL_ASM-style tools such as asm_processor, but projects rarely use them. So:

- **Function level:** each match is verified in isolation by compiling the candidate and diffing that one symbol against the vanilla build's TU object. Matches are stored in the DB.
- **TU level:** once all functions in a TU match, the integrator assembles the C file (functions in address order plus that TU's data), swaps the TU in the link-order file, rebuilds **every** version, and checks every hash.

Partial-TU integration through a GLOBAL_ASM tool is a Phase 5 option for TUs that stall.

### D5. The worker sandbox is minimal by construction
A worker sees: `TASK.md`, `target.s`, `ctx.h`, `examples/`, an optional `draft.c`, `LEARNINGS.md`, `candidate.c`, and `./check`. It cannot see the target repo. This keeps tokens down, removes the temptation (and the ability) to "fix" the match by editing headers, flags or the target, and makes attempts reproducible.

### D6. The orchestrator never trusts the worker
A worker's claim of "matched" means nothing. The orchestrator re-runs `rematch cd` on the final `candidate.c` and only that result counts. The same goes for readability lints.

### D7. Benchmark first, and contamination-aware
Public decomp code is likely in model training data, so a benchmark built from old human C would overstate real performance. The default benchmark pool is **functions humans decompiled after `BENCH_CUTOFF_DATE`** (default 2026-07-01). We find them from git history: their addresses disappear from assembly. Everything else is reported separately as "possibly contaminated". Bench sets are frozen by seed so that prompt and context changes are compared on identical functions.

### D8. Readability is a gate, not a nicety
A match is accepted only if it passes lints: no inline asm, no new pragmas, no gratuitous `volatile`/`goto`/`do{}while(0)`, no `register` keyword abuse, and an identifier policy (see SPEC §9). Ugly matches poison future context (lesson from Lewis) and defeat the point of having source.

### D9. Learnings are curated, not append-only
Workers propose learnings in `NOTES.md`. They land in a candidates table. A curation pass merges good ones into the rule files, keeping them short and deduplicated. An ever-growing learnings file turns into noise and burns tokens on every attempt. Curation is an agent, but its claims are not trusted: each rule needs a pair of C snippets whose compiled code differs the way the rule says, and rematch recompiles that pair before accepting the rule and before every run (SPEC §13.1). Humans can still accept rules by hand.

### D10. Scheduling: easiest first, similarity-boosted, escalate the stuck
Difficulty comes from size, branches, switch tables, literal pool size and unknown callees, minus a bonus for having a highly similar matched function. Untried functions go in difficulty order. Stuck functions get retried later with escalation: a stronger model, higher effort, TU-context compile, alternate drafts.

### D11. Cross-repo references are a lever
Related decomp projects (same engine family or same compiler) often contain near-identical functions in C. Retrieving the most similar function from those repos, by opcode-sequence similarity (which requires building them once), should help a lot. In benchmark reports this is tracked as a separate ablation so its effect is visible.

### D12. The core is target-agnostic
Everything rematch knows about a target comes from the target repo's `.rematch/` directory: build commands, expected hashes, forbidden push URLs, and the facts in TARGET.md. rematch has no per-game code paths and this repo holds no target data, host names or personal setup. Adding a target means writing its `.rematch/`, not changing rematch. Machine-specific settings (paths, a container prefix for toolchains that can't run natively) live in the gitignored `.env`.

## 5. Risks

| Risk | Mitigation |
|---|---|
| Token cost and rate limits dominate | Per-attempt turn and budget caps, stagnation stop, difficulty ordering, a cheap default model with escalation, prompt caching via a stable system prompt |
| Long tail stalls (as seen in prior art) | Phase 5 escalation ladder, TU-context compiles, human review queue. Accept that 100% may not happen. |
| File-level IPA / inlining makes codegen depend on neighboring functions | TU-context compile mode (SPEC §6.4) |
| Version-specific code (version conditionals in assembly or C) | Every match is verified for every version when the function's TU contains version conditionals. Integration builds every version. |
| Target fork diverges from an active upstream | Periodic upstream merges into `main`, rebasing `rematch/live`. Functions humans decompiled get marked `superseded`. |
| Benchmark contamination | Cutoff-based pool (D7) |
| Toolchain only runs natively on some hosts | `TOOLCHAIN_EXEC` prefix (container or emulation), with compile speed measured against the Phase 1 target before choosing a worker host |
| Legal | Target repos stay private, games are your own, no assets or ROMs committed or shared |

## 6. What "done" looks like

- A frozen benchmark with a credible match rate and a known cost per match.
- A `rematch/live` branch where every version's hash passes, with a large and growing number of TUs converted to readable C.
- A harness that can be pointed at a new target with only a new `.rematch/` directory.
