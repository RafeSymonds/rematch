-- rematch schema, SPEC §5. Must equal the result of applying every migration (tested).

CREATE TABLE functions (
  id              INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,            -- current name in target
  address         INTEGER NOT NULL,
  module          TEXT NOT NULL,            -- module or overlay name as the target defines it
  tu_path         TEXT NOT NULL,            -- e.g. 'asm/foo.s' or 'src/foo.c'
  source_kind     TEXT NOT NULL CHECK (source_kind IN ('asm','c_human')),
  mode            TEXT NOT NULL,            -- ISA mode as the target defines it, e.g. 'thumb', 'arm'
  n_insns         INTEGER,
  n_branches      INTEGER,
  has_switch      INTEGER DEFAULT 0,
  pool_entries    INTEGER,
  callees_json    TEXT,                     -- ["memset", "sub_02096780", ...]
  version_cond    INTEGER DEFAULT 0,        -- TU contains version conditionals
  difficulty      REAL,
  status          TEXT NOT NULL DEFAULT 'untried'
                  CHECK (status IN ('untried','leased','matched','failed','stuck',
                                    'integrated','superseded','skipped')),
  escalation      INTEGER NOT NULL DEFAULT 0,
  attempts        INTEGER NOT NULL DEFAULT 0,
  best_score      REAL,
  decompiled_at   TEXT,                     -- c_human only: ISO date it left assembly (§11.1)
  lease_owner     TEXT, lease_expires TEXT,
  updated_at      TEXT NOT NULL,
  UNIQUE (module, address)
);

CREATE TABLE runs (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('bench','live','selftest')),
  bench_set   TEXT,
  config_json TEXT NOT NULL,                -- full resolved config + prompt_version + git SHAs + target.toml hash
  started_at  TEXT NOT NULL, ended_at TEXT, notes TEXT
);

CREATE TABLE attempts (
  id             INTEGER PRIMARY KEY,
  run_id         INTEGER NOT NULL REFERENCES runs(id),
  function_id    INTEGER REFERENCES functions(id),
  task_kind      TEXT NOT NULL CHECK (task_kind IN ('function','tu_data','tu_fix',
                                                     'type_merge','naming')),
  model          TEXT, effort TEXT, escalation INTEGER,
  prompt_version INTEGER, context_version INTEGER,
  started_at     TEXT, ended_at TEXT,
  outcome        TEXT CHECK (outcome IN ('match','nonmatch','compile_fail','stagnation',
                                         'check_limit','turn_limit','timeout','budget',
                                         'invalid','error')),
  checks_used    INTEGER, best_score REAL, final_score REAL,
  num_turns      INTEGER, input_tokens INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cost_usd REAL,  -- client-side estimate from result message
  sandbox_path   TEXT, transcript_path TEXT, best_candidate_path TEXT
);

CREATE TABLE matches (
  function_id      INTEGER PRIMARY KEY REFERENCES functions(id),
  attempt_id       INTEGER NOT NULL REFERENCES attempts(id),
  c_source         TEXT NOT NULL,
  verified_versions TEXT NOT NULL,           -- comma-separated version names
  lint_json        TEXT NOT NULL,            -- §9 results (must be all-pass)
  verified_at      TEXT NOT NULL,
  integrated_commit TEXT
);

CREATE TABLE learning_candidates (
  id INTEGER PRIMARY KEY, attempt_id INTEGER REFERENCES attempts(id),
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
  created_at TEXT NOT NULL
);

CREATE TABLE bench_sets (
  id TEXT PRIMARY KEY,                       -- e.g. 'b100-s42-c20260701'
  function_ids_json TEXT NOT NULL, seed INTEGER, cutoff TEXT, created_at TEXT NOT NULL
);

-- Type recovery (§16). Tables exist from Phase 0; they fill from Phase 2 on.
CREATE TABLE shared_types (
  id               INTEGER PRIMARY KEY,
  name             TEXT NOT NULL UNIQUE,     -- UnkStruct_<ADDR> until the naming pass renames it
  kind             TEXT NOT NULL CHECK (kind IN ('struct','union')),
  size             INTEGER,                  -- sizeof, measured by the compiler (§16.1)
  definition       TEXT NOT NULL,            -- C definition injected into ctx.h
  fields_json      TEXT NOT NULL,            -- [{"offset": 44, "size": 2, "type": "u16", "name": "unk_2C"}, ...]
  origin           TEXT NOT NULL CHECK (origin IN ('merge','reference')),
  status           TEXT NOT NULL DEFAULT 'proposed'
                   CHECK (status IN ('proposed','accepted','rejected','superseded')),
  merge_attempt_id INTEGER REFERENCES attempts(id),
  verified_matches INTEGER,                  -- matches re-verified at 100% against this definition
  header           TEXT,                     -- header it was written to on rematch/live (§10)
  created_at       TEXT NOT NULL,
  decided_at       TEXT
);

CREATE TABLE provisional_types (
  id             INTEGER PRIMARY KEY,
  function_id    INTEGER NOT NULL REFERENCES functions(id),  -- the match that declared it
  attempt_id     INTEGER REFERENCES attempts(id),
  name           TEXT NOT NULL,              -- UnkStruct_<ADDR>[_N]
  kind           TEXT NOT NULL CHECK (kind IN ('struct','union')),
  size           INTEGER,                    -- measured sizeof
  fields_json    TEXT NOT NULL,              -- measured offsets and sizes, as in shared_types
  signature      TEXT NOT NULL,              -- canonical "offset:size,..." string for clustering
  source         TEXT NOT NULL,              -- the declaration as the worker wrote it
  shared_type_id INTEGER REFERENCES shared_types(id),  -- set when merged
  created_at     TEXT NOT NULL,
  UNIQUE (function_id, name)
);

CREATE TABLE type_uses (                     -- which matches compile against which shared type
  shared_type_id INTEGER NOT NULL REFERENCES shared_types(id),
  function_id    INTEGER NOT NULL REFERENCES functions(id),
  PRIMARY KEY (shared_type_id, function_id)
);

CREATE TABLE name_proposals (                -- naming pass (§16.5); applied only after human approval
  id             INTEGER PRIMARY KEY,
  subject        TEXT NOT NULL CHECK (subject IN ('function','type','field')),
  function_id    INTEGER REFERENCES functions(id),
  shared_type_id INTEGER REFERENCES shared_types(id),
  field_offset   INTEGER,                    -- subject 'field' only
  current_name   TEXT NOT NULL,
  proposed_name  TEXT NOT NULL,
  evidence_json  TEXT NOT NULL,              -- strings, callees, constants, reference-repo equivalents
  attempt_id     INTEGER REFERENCES attempts(id),
  status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
  created_at     TEXT NOT NULL,
  decided_at     TEXT
);
