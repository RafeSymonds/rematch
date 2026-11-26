# verify-log

Evidence for resolved rematch VERIFY items. Paths are shown relative to `~` and repo names are
generic, because this repo holds no machine or target details.

## 2026-11-26: SPEC §7.3, worker isolation and configuration leakage (Phase 2)

Claude Code 2.1.332, subscription login, `--model haiku`, a throwaway sandbox under
`REMATCH_WORKDIR`. Each probe was 1–8 turns (total < $0.01 estimated).

Base flags: `-p ... --tools Read,Edit,Write,Bash --allowedTools Read "Edit(candidate.c)"
"Write(candidate.c)" "Bash(./check)" --permission-mode dontAsk --permission-prompts none
--strict-mcp-config --disable-slash-commands --no-session-persistence --output-format stream-json`.

**P1: `system/init` with and without `--setting-sources project,local`**
```
default:          output_style "Concise" (user's), agents [+3 user agents], plugins [4 user plugins + builtins], skills [], mcp_servers []
project,local:    output_style "default", agents [built-ins only], plugins [builtins only], skills [], mcp_servers []
both:             result success ("OK"), apiKeySource "none" (subscription login works)
```

**P2: user CLAUDE.md** (prompt: quote any CLAUDE.md/memory in context)
```
default:        "Yes. The global `~/.claude/CLAUDE.md` is in my context. ..."
project,local:  "Yes. The user-level file `~/.claude/CLAUDE.md` is in context. ..."
```
The user memory loads in subscription mode either way. This is recorded as a known confound (SPEC §7.3).

**P3: reads, writes and shell outside the sandbox** (`--setting-sources project,local`)
```
Read ~/.rematch/outside_probe.txt   -> allowed ("secret-outside-line")
Read /etc/hosts                     -> allowed
Write ~/.rematch/outside_write.txt  -> denied (don't ask mode); file not created
Bash "ls /"                         -> denied
Bash "./check"                      -> allowed ("check ok (probe)")
Write candidate.c                   -> allowed
Write target.s                      -> denied
```

**P3b: deny rules** (`--disallowedTools "Read(//<projects dir>/**)" "Read(~/.rematch/outside_probe.txt)"
"Read(task.json)" "Read(.target/**)"`)
```
Read <vanilla worktree>/src/<file>.c      -> denied ("File is in a directory that is denied by your permission settings.")
Read ~/.rematch/outside_probe.txt         -> denied
Read task.json                            -> denied
Read .target/x.json                       -> denied
Read candidate.c                          -> allowed
Read <rematch repo>/README.md             -> denied
```

**P4: `--max-turns 1`** with a task needing two turns: result subtype `error_max_turns`, exit 1.
`--help` no longer lists the flag, but it works. Also `Bash("./check; echo ...")` was denied,
because compound commands don't match `Bash(./check)`.

**P5: `--effort low`**: accepted, result success.

Result: SPEC §7.3 VERIFY resolved. Workers run with `--setting-sources project,local`, scoped
allow rules, and read-deny rules for the target repo, the vanilla worktree, rematch itself,
`REMATCH_VAR`, answer-bearing workdir folders, `task.json` and `.target/`.
