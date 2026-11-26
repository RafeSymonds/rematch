-- 0002: type recovery (SPEC §5, §16): new tables, and type_merge/naming task kinds.
-- SQLite cannot alter a CHECK constraint, so attempts is rebuilt (store.migrate runs this
-- with foreign keys off and checks them before commit).

CREATE TABLE attempts_new (
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

INSERT INTO attempts_new SELECT * FROM attempts;
DROP TABLE attempts;
ALTER TABLE attempts_new RENAME TO attempts;

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
