-- Plain pgvector schema (portable profile, design D-3).
-- No AlloyDB-specific features: runs identically on pgvector container, Cloud SQL, AlloyDB.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   TEXT PRIMARY KEY,          -- "<source_id>#<seq>"
    source_id  TEXT NOT NULL,
    space_key  TEXT NOT NULL,
    title      TEXT NOT NULL,
    heading    TEXT,
    breadcrumb TEXT,
    content    TEXT NOT NULL,
    embedding  vector(768) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chunks_space_key_idx ON chunks (space_key);
CREATE INDEX IF NOT EXISTS chunks_source_id_idx ON chunks (source_id);
-- Vector index deferred: MVP corpus is small; add HNSW when corpus grows.
