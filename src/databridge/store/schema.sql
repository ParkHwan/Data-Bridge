-- Plain pgvector schema (portable profile, design D-3).
-- No AlloyDB-specific features: runs identically on pgvector container, Cloud SQL, AlloyDB.
-- pg_trgm is stock contrib (present in pgvector/pgvector image, Cloud SQL, AlloyDB) —
-- unlike pg_bigm, which is unavailable on the pgvector image and would break D-3.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

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
-- Trigram GIN index: language-agnostic substring recall (Korean josa variants,
-- English typos/partial matches) fused as a third RRF signal alongside the english
-- tsvector path. The stock parser cannot segment Korean morphology; character
-- trigrams sidestep it without a morphological analyzer.
CREATE INDEX IF NOT EXISTS chunks_content_trgm_idx ON chunks USING GIN (content gin_trgm_ops);
-- Vector index deferred: MVP corpus is small; add HNSW when corpus grows.
