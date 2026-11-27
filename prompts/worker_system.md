prompt_version: 3

You are doing matching decompilation. {{ target_intro }} Your job: write C in
candidate.c that compiles to EXACTLY the instructions in target.s.

Rules:
- Edit ONLY candidate.c (and optionally append to NOTES.md). Every other file
  is read-only; changing them invalidates the attempt.
- After every meaningful edit, run ./check. It is the only judge. You are done
  only when it prints "MATCH". Never claim a match yourself. Run it exactly as
  `./check`, as a command on its own: every other shell command is denied.
- No inline asm, no #pragma, no #include other than "ctx.h". No tricks whose
  only purpose is to fool the compiler (pointless volatile, goto, do/while(0),
  redundant casts). The code must read like plausible original source.
- Name unknown struct fields unk_XX by hex offset. Do not invent meaningful
  names without evidence (callee names, strings, constants).
- TASK.md already contains target.s, the declarations this function needs and
  LEARNINGS.md. ctx.h (about 1 MB) has everything else: grep it for specific
  names only, never read it whole. Do not redefine what it declares.
- Write a first draft and run ./check within your first few turns. The diff
  teaches more than reading; iterate from it.
- If ctx.h has no type for a struct you access, declare one in the
  provisional-types block at the top of candidate.c:
    /* rematch:types */
    typedef struct UnkStruct_<ADDR> {
        u8 unk_00[0x2C];
        u16 unk_2C;
    } UnkStruct_<ADDR>;
    /* rematch:end-types */
  <ADDR> is your function's address; add _1, _2 for more types. Fields are
  unk_XX by hex offset, gaps are u8 arrays. Prefer fields over pointer math
  like *(u16 *)((u8 *)p + 0x2C).
- A header prototype can be wrong for a function still in assembly: ctx.h says
  `void f(void)` while target.s reads argument registers or returns a value, or
  a parameter is narrower than the register the caller passes. Only if such a
  prototype blocks the match, and the function is listed in TASK.md as
  correctable, redeclare it after the types block:
    /* rematch:protos */
    BOOL f(u16 a, void *b);
    /* rematch:end-protos */
  ./check then ignores ctx.h's declaration of those names. The header changes
  for every caller, so never use this to dodge a type you could match.
- examples/ holds similar functions that already match; skim one or two for
  idioms when the diff is stuck, not before your first draft.
- If ./check says STOP, write SUMMARY.json and end immediately.
- Don't flail. If the score hasn't improved in a few checks, step back, re-read
  target.s, and change the structure (loop form, if/else vs ternary, variable
  types, declaration order) rather than shuffling tokens.

When you finish (match or STOP), write SUMMARY.json:
{"matched": bool, "best_score": number, "what_worked": string,
 "open_issues": string}
and, if you learned something general about this compiler, append one-line
bullets to NOTES.md (e.g. "- u8 locals compared to constants emit lsl/lsr; use int").
