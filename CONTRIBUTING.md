# CONTRIBUTING

rematch is built with coding agents. This file covers the workflow for humans and agents.

## Scope reminder

- rematch output goes **to this repo and your own target repos only**. Many decomp projects prohibit LLM-generated contributions. Respect that completely: no PRs, issues, or community posts of rematch output.
- Upstream work flows *into* a target fork via `rematch sync-upstream`. Nothing flows back.
- This repo is target-agnostic. Target facts live in the target's `.rematch/`, machine settings in `.env`.

## Branches and commits

- `main`: always green (`pytest`, `ruff`, `mypy`).
- `phase-N-<slug>`: one per roadmap phase or sub-feature. Merge to main when the work is green and the phase checklist is updated.
- In a **target repo**: `main` tracks upstream merges. `rematch/live` holds integrated TUs only, written only by `rematch integrate`.
- Commit messages: imperative summary, then the body notes `SPEC: §x.y updated` when contracts change, and `prompt_version`/`context_version` bumps.

## Prompt and context versioning

- `prompts/worker_system.md` starts with `prompt_version: N`. Bump N for **any** wording change.
- `context_version` lives in `src/rematch/context/__init__.py`. Bump it for any change to what goes into a sandbox (ctx builder, examples selection, draft seeding, learnings injection).
- Both versions are stored on every attempt and run, so results stay attributable.

## Benchmark hygiene

- Compare changes **only** on frozen sets, by default `b100-s42-c20260701` (clean pool), confirmed on a second set before claiming a win.
- Change one thing per comparison. If you must bundle changes, say so in the log.
- `rematch bench` appends one row per run to the target's `.rematch/benchmarks.md` (rows name the target, so they stay out of this repo):

```
| date | target | run | set | prompt_v | context_v | model:effort | match % (S/M/L) | med checks | tokens/match | $/match (est) | notes |
```

- Never delete rows. Bad results are data too.

## Learnings curation

- Workers propose learnings in `NOTES.md`. They land in `learning_candidates`.
- Curate with `rematch learnings curate --yes` (an agent that must prove each rule with an evidence pair that rematch recompiles) or by hand with `rematch learnings review`. Accept only rules that are general (not about one function), true for this compiler and flags, and not already covered.
- `docs/learnings/<compiler>.json` holds compiler rules only. Anything that names a target's functions, types or files belongs in that target's `.rematch/learnings.json`, and curation moves such rules automatically.
- Keep each scope under 150 rules. `rematch learnings verify --retire` removes rules whose evidence no longer holds.

## Review checklist (for agent-written changes)

- [ ] Doesn't touch the vanilla worktree or write to the target outside `rematch integrate` and `.rematch/`
- [ ] No path can invoke the real `claude` binary from tests
- [ ] Checker semantics unchanged, or deliberately changed with SPEC §6.5 updated and golden tests extended
- [ ] New hints and lints have fixtures
- [ ] Version bumps noted; benchmark command provided if worker behavior changed
- [ ] No ROMs, BIOS files, saves or secrets in the diff
- [ ] No target names, target paths, host names or personal setup in the diff
