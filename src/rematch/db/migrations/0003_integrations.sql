-- 0003: run-based TU integration (SPEC §10, Phase 4a).
CREATE TABLE integrations (
  id             INTEGER PRIMARY KEY,
  tu_path        TEXT NOT NULL,             -- the assembly TU, as in functions.tu_path
  status         TEXT NOT NULL CHECK (status IN ('committed','failed')),
  functions_json TEXT NOT NULL,             -- every function of the TU in C after this step
  pieces_json    TEXT NOT NULL,             -- the TU's link-order pieces after this step
  commit_sha     TEXT,
  detail         TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX integrations_tu ON integrations (tu_path, id);
