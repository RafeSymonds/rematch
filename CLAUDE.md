# CLAUDE.md

@AGENTS.md

The import above holds the project rules and traps. Everything below is specific to working on rematch in Claude Code.

## How to work here

- **Start each roadmap phase in plan mode.** Read the phase in ROADMAP.md, the SPEC sections it touches, and the target's `.rematch/TARGET.md`. Propose a short plan listing the files to create, the tests to add and any VERIFY items to resolve. Wait for the maintainer's go-ahead, then implement.
- **One phase, one branch:** `phase-N-<slug>`. Small commits. Commit messages say which SPEC sections changed, if any.
- **Resolve VERIFY items with evidence.** Run the command and paste the relevant output into the commit message or the verify log, then update the spec and remove the marker. rematch items go in `docs/verify-log.md` and SPEC.md. Target items go in the target's `.rematch/verify-log.md` and TARGET.md.
- **Use `rematch ctx <FUNCTION> --out /tmp/sbx` to inspect sandboxes** instead of reasoning about them. Read what a worker would actually see.

## Token and cost discipline

- Never invoke the real `claude` binary from code you're testing, and never run `rematch attempt`, `bench` or `run` unless the maintainer asks in this session. Those commands require `--yes` for a reason.
- Don't use subagents to "try a few functions". Real worker runs only go through the orchestrator so they're logged, budgeted and comparable.
- When a change needs a benchmark, say so and give the exact command (with the frozen set ID). Let the maintainer run it.

## Hosts

- Build and worker hosts are machine-specific. Their settings (`TARGET_REPO`, `TOOLCHAIN_EXEC`, paths) live only in `.env`. Never hardcode or commit them.
- Measure `./check` speed on a host before making it a worker host (Phase 1 target: median ≤ 3 s).

## Quick traps reminder (full list in AGENTS.md)

- Workers live in sandboxes, never in a repo. Never trust worker claims; re-verify.
- Targets come from the pinned vanilla worktree. Compile argv comes from the build, not guesses.
- Verify every version for TUs with version conditionals. Integrate whole TUs, and edit only the link-order source file.
- Benchmarks use the clean post-cutoff pool on frozen sets.
- Nothing ever goes to a target's upstream. Nothing target-specific goes in this repo.
