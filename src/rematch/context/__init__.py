"""Context building (SPEC §6.3)."""

# Bump on any change to what goes into a sandbox (CONTRIBUTING.md).
# 2: task.json moved out of the sandbox. 3: TU -I flags for the context command.
# 4: TASK.md inlines target.s, the needed declarations and LEARNINGS (Phase 3, A1).
# 5: similarity-ranked examples, none decompiled with or after a bench target (Phase 3, B1).
# 6: m2c draft.c seeds candidate.c when M2C is set (Phase 3, B2).
# 7: LEARNINGS = rematch's generic rules + the target's .rematch/LEARNINGS.md (Phase 3, B3).
CONTEXT_VERSION = 7
