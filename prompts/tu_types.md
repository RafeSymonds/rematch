tu_types_version: 1

You are cleaning up a matching decompilation. {{ target_intro }} candidate.c holds several
functions from one file. Each function was matched on its own, and each declared the structs
it needed as provisional types (`UnkStruct_<ADDR>`, fields `unk_XX` by hex offset). Many of
these describe the same object: a work struct passed between the functions, a struct one
function allocates and another reads. Merge them so the file has one typedef per object.

How to work:
- Find types that are the same object: a function passes its typed pointer to a sibling whose
  parameter has another provisional type; two functions declare the same extern with
  different types; two types have pointer fields at the same offset to further duplicates.
- Write one typedef per object, keeping one of the existing names. Its fields are the union
  of the fields the functions use, at their offsets, with `u8 unk_XX[N]` padding between them.
  A field's offset and width must not change: the generated code depends on them. Where two
  functions model the same bytes differently (an array in one, a scalar in another; an
  embedded struct in one, a scalar in another), pick the form that makes both read naturally
  and adjust the accesses.
- Then delete the duplicates, rename every use, and drop casts that only existed to convert
  between duplicates.
- Don't merge types that only look alike. Without evidence that they are the same object,
  leave them separate.

Rules: only edit candidate.c. Keep every function body's logic as it is: change types, names,
casts and declarations only. No inline asm, no #pragma, no new #include lines. Keep any
mode-switch lines the file already has around functions.

./check compiles the file for every version and prints each function's status and the number
of provisional typedefs. It prints MATCH when every function still matches and the file has
fewer provisional typedefs than it started with. Merge as many true duplicates as you can
before you stop. When done, write SUMMARY.json: {"matched": bool, "what_changed": "...",
"open_issues": "..."} and end.
