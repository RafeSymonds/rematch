tu_fix_version: 1

You are finishing a matching decompilation. {{ target_intro }} Several functions from one
assembly file were each matched on their own: each compiled to exactly the original machine
code. candidate.c now holds all of them as one C file, and it must compile as a whole with
every function still byte-identical. Usually only a few lines need to change.

Typical problems:
- Two functions declare the same extern or prototype differently. Pick the declaration that
  fits every use (and any header that already declares it), then adjust the code that relied
  on the other one (a cast, a different local type) without changing the generated code.
- A function guessed a sibling's signature, and the real definition differs. The prototypes at
  the top come from the definitions; keep them and fix the calls.
- Two provisional types (`UnkStruct_<ADDR>`) describe the same object. Merge them into one
  typedef whose `unk_XX` fields cover both uses, keep one of the names, and update the uses.
- A header declares a function with a different signature than its definition here. The
  header wins: change the definition and its body so the code still matches. Headers can't be
  edited.

Rules: only edit candidate.c. No inline asm, no #pragma, no new #include lines, no data
definitions (data stays in the assembly file: declare it extern). Don't rewrite function
bodies beyond what the fix needs.

./check compiles the file for every version and prints each function's status, with a diff
for any that no longer match. It prints MATCH when everything matches. Run it after each
change. When done, write SUMMARY.json: {"matched": bool, "what_changed": "...",
"open_issues": "..."} and end.
