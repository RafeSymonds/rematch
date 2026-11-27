tu_data_version: 1

You are finishing a matching decompilation. {{ target_intro }} Every function of one
assembly file is already C, in candidate.c, and matches. The file's data is still assembly,
in data.s. Define that data in C, in candidate.c, so the compiled object's data sections are
byte-identical to data.s and every function still matches. Then the assembly file can go.

How to work:
- Replace each `extern` declaration of a data.s label with a definition. Choose real types
  that fit how the functions use the data and what the bytes are (structs, arrays of structs,
  tables of function pointers, strings). Prefer designated initializers for structs.
- Labels that other files use (./check lists them) keep their exact names and stay global.
  Other labels may become `static`, and one C object may span several labels (a label in the
  middle of a table usually marks an element, not a new object).
- Data in a writable section (`.data`, `.bss`) must not be `const`. Zero-filled space is a
  zero-initialized definition.
- The compiler decides where each object goes. It may not keep source order: it can sort
  objects (for example by size) and give odd-sized objects their own section. ./check prints
  a layout table of the target's labels next to your symbols. Reorder definitions, or merge
  or split objects, until the table lines up.
- If a function stops matching after you define data it reads, the compiler may be folding
  loads of constant data; look at the function's diff before changing types.

Rules: only edit candidate.c. Don't change function bodies beyond what the data needs. No
inline asm, no #pragma, no new #include lines.

./check compiles candidate.c for every version, compares each data section (bytes,
relocations, alignment) and every function. It prints MATCH when everything matches. Run it
after each change. When done, write SUMMARY.json: {"matched": bool, "what_changed": "...",
"open_issues": "..."} and end.
