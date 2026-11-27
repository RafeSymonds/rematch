-- 0005: batched integration and data pieces (SPEC §10, Phase 4b).
-- plan_hash: hash of a TU step's inputs; a TU whose last failure had the same hash is skipped.
-- piece_sources.kind: 'functions' (tu_fix) or 'data' (tu_data, the TU's C file with its data).
ALTER TABLE integrations ADD COLUMN plan_hash TEXT;
ALTER TABLE piece_sources ADD COLUMN kind TEXT NOT NULL DEFAULT 'functions';
