-- Plain pgvector schema (portable profile, design D-3).
-- No AlloyDB-specific features: runs identically on pgvector container, Cloud SQL, AlloyDB.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    space_key  TEXT NOT NULL,
    chunk_id   TEXT NOT NULL,             -- "<source_id>#<seq>"
    source_id  TEXT NOT NULL,
    title      TEXT NOT NULL,
    heading    TEXT,
    breadcrumb TEXT,
    content    TEXT NOT NULL,
    embedding  vector(768) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Composite PK: the same source_id may legitimately exist in different spaces —
    -- space isolation applies to mutations, not only to search (post-review P1).
    PRIMARY KEY (space_key, chunk_id)
);

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX IF NOT EXISTS chunks_space_source_idx ON chunks (space_key, source_id);
CREATE INDEX IF NOT EXISTS chunks_content_tsv_idx ON chunks USING GIN (content_tsv);
-- Vector index deferred: MVP corpus is small; add HNSW when corpus grows.
