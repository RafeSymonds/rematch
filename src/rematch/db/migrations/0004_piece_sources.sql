-- 0004: C pieces repaired by tu_fix (SPEC §10, Phase 4a). The integrator uses a stored source
-- when a planned piece holds exactly these functions.
CREATE TABLE piece_sources (
  id             INTEGER PRIMARY KEY,
  tu_path        TEXT NOT NULL,
  functions_json TEXT NOT NULL,             -- the piece's functions, address order
  source         TEXT NOT NULL,
  attempt_id     INTEGER REFERENCES attempts(id),
  created_at     TEXT NOT NULL
);
CREATE INDEX piece_sources_tu ON piece_sources (tu_path, functions_json);
