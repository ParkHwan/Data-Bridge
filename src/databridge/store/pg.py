"""pgvector store: vector and RRF hybrid search with metadata filters.

Hybrid search fuses three ranked candidate lists via reciprocal rank fusion:

1. **vector** — cosine distance over embeddings.
2. **english FTS** — ``to_tsvector('english', ...)`` keyword recall for English.
3. **trigram** — ``pg_trgm`` ``word_similarity`` over raw content, a language-agnostic
   substring signal. The stock text-search parser cannot segment Korean morphology
   (조사 교착: ``릴리스를`` / ``릴리스가`` tokenize as distinct words), so English FTS
   alone gives weak Korean keyword recall. Character trigrams sidestep morphology
   without a language-specific analyzer, recovering Korean recall (and catching English
   typos / partial matches) while the english FTS path keeps English quality intact.

Space isolation is a plain ``WHERE space_key = %s``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from typing import Any

import psycopg
from pgvector.psycopg import register_vector

from databridge.embed.base import EMBEDDING_DIM, EmbeddingProfile
from databridge.ingest.chunker import Chunk
from databridge.store.exceptions import EmbeddingProfileMismatchError
from databridge.store.provenance import Generation, GenerationState, profile_id_for


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk_id: str
    source_id: str
    space_key: str
    title: str
    heading: str | None
    breadcrumb: str | None
    content: str
    distance: float
    rrf_score: float | None = None
    fts_rank: int | None = None
    trgm_rank: int | None = None


class PgVectorStore:
    def __init__(self, dsn: str, *, profile: EmbeddingProfile | None = None) -> None:
        self._dsn = dsn
        self._profile = profile

    def _connect(self, *, register: bool = True) -> psycopg.Connection:
        conn = psycopg.connect(self._dsn)
        if register:
            register_vector(conn)
        return conn

    def ensure_schema(self) -> None:
        schema = resources.files("databridge.store").joinpath("schema.sql").read_text("utf-8")
        # register=False: the vector type does not exist until this very statement
        # creates the extension, and register_vector fails on a fresh database.
        with self._connect(register=False) as conn:
            conn.execute(schema)

    def replace_source(
        self,
        *,
        space_key: str,
        source_id: str,
        chunks: list[Chunk],
        embeddings: list[list[float]],
    ) -> int:
        """Atomically replace all chunks of one source within one space.

        Delete + insert run in a single transaction (post-review P1: separate
        delete/upsert calls could leave a source empty on mid-ingest failure).
        """
        self._validate_batch(chunks, embeddings)
        for chunk in chunks:
            if chunk.space_key != space_key or chunk.source_id != source_id:
                msg = f"chunk {chunk.chunk_id} does not belong to {space_key}/{source_id}"
                raise ValueError(msg)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM chunks WHERE space_key = %s AND source_id = %s",
                (space_key, source_id),
            )
            self._insert_rows(cur, chunks, embeddings)
        return len(chunks)

    def upsert_chunks(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int:
        self._validate_batch(chunks, embeddings)
        with self._connect() as conn, conn.cursor() as cur:
            self._insert_rows(cur, chunks, embeddings)
        return len(chunks)

    def delete_source(self, *, space_key: str, source_id: str) -> int:
        """Space-scoped delete — mutations honor space isolation (post-review P1)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM chunks WHERE space_key = %s AND source_id = %s",
                (space_key, source_id),
            )
            return cur.rowcount or 0

    def list_source_ids(self, *, space_key: str) -> set[str]:
        """Return the distinct sources currently stored in one isolated space."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT source_id FROM chunks WHERE space_key = %s",
                (space_key,),
            )
            return {str(row[0]) for row in cur.fetchall()}

    @contextmanager
    def advisory_lock(self, key: str) -> Iterator[bool]:
        """Hold a session-level advisory lock for the entire context lifetime."""
        with self._connect(register=False) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (key,))
            row = cur.fetchone()
            acquired = bool(row and row[0])
            try:
                yield acquired
            finally:
                if acquired:
                    cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))

    @staticmethod
    def _lock_space_for_update(cur: psycopg.Cursor, space_key: str) -> None:
        """Serialize provenance mutations for one space until transaction end."""
        lock_key = f"databridge:embedding-profile:{space_key}"
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))

    @staticmethod
    def _ensure_profile(cur: psycopg.Cursor, profile: EmbeddingProfile) -> str:
        profile_id = profile_id_for(profile)
        cur.execute(
            """
            INSERT INTO embedding_profile
                (profile_id, provider, model, dimension, config_fingerprint)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                profile_id,
                profile.provider,
                profile.model,
                profile.dimension,
                profile.config_fingerprint,
            ),
        )
        cur.execute(
            """
            SELECT provider, model, dimension, config_fingerprint
              FROM embedding_profile
             WHERE profile_id = %s
            """,
            (profile_id,),
        )
        row = cur.fetchone()
        expected = (
            profile.provider,
            profile.model,
            profile.dimension,
            profile.config_fingerprint,
        )
        if row is None or tuple(row) != expected:
            raise EmbeddingProfileMismatchError("Stored embedding profile identity is inconsistent")
        return profile_id

    @staticmethod
    def _generation_from_row(row: tuple[Any, ...]) -> Generation:
        return Generation(
            generation_id=int(row[0]),
            space_key=str(row[1]),
            profile=EmbeddingProfile(
                provider=str(row[2]),
                model=str(row[3]),
                dimension=int(row[4]),
                config_fingerprint=str(row[5]),
            ),
            state=GenerationState(str(row[6])),
        )

    @classmethod
    def _get_generation(
        cls,
        cur: psycopg.Cursor,
        *,
        space_key: str,
        generation_id: int,
        for_update: bool = False,
    ) -> Generation | None:
        lock_clause = "FOR UPDATE" if for_update else ""
        cur.execute(
            f"""
            SELECT g.generation_id, g.space_key, p.provider, p.model, p.dimension,
                   p.config_fingerprint, g.state
              FROM space_generation g
              JOIN embedding_profile p ON p.profile_id = g.profile_id
             WHERE g.space_key = %s AND g.generation_id = %s
             {lock_clause}
            """,
            (space_key, generation_id),
        )
        row = cur.fetchone()
        return None if row is None else cls._generation_from_row(row)

    @classmethod
    def _get_active_generation(
        cls, cur: psycopg.Cursor, *, space_key: str, for_update: bool = False
    ) -> Generation | None:
        lock_clause = "FOR UPDATE OF g" if for_update else ""
        cur.execute(
            f"""
            SELECT g.generation_id, g.space_key, p.provider, p.model, p.dimension,
                   p.config_fingerprint, g.state
              FROM space_generation g
              JOIN embedding_profile p ON p.profile_id = g.profile_id
             WHERE g.space_key = %s AND g.state = 'active'
             {lock_clause}
            """,
            (space_key,),
        )
        row = cur.fetchone()
        return None if row is None else cls._generation_from_row(row)

    def _require_profile(self) -> EmbeddingProfile:
        if self._profile is None:
            raise RuntimeError("Embedding profile is required for vector operations")
        return self._profile

    @staticmethod
    def _assert_matching_profile(generation: Generation, profile: EmbeddingProfile) -> None:
        if generation.profile != profile:
            raise EmbeddingProfileMismatchError(
                "Embedding profile does not match the target generation"
            )

    def _resolve_write_generation(
        self,
        cur: psycopg.Cursor,
        *,
        space_key: str,
        generation_id: int | None,
        allow_empty_bootstrap: bool,
    ) -> Generation:
        """Resolve and validate a write target while the space xact lock is held."""
        profile = self._require_profile()
        if generation_id is not None:
            generation = self._get_generation(
                cur,
                space_key=space_key,
                generation_id=generation_id,
                for_update=True,
            )
            if generation is None:
                raise EmbeddingProfileMismatchError(
                    "Target embedding generation does not exist in this space"
                )
            self._assert_matching_profile(generation, profile)
            if generation.state is not GenerationState.BUILDING:
                raise EmbeddingProfileMismatchError(
                    "Explicit write target must be a building generation"
                )
            return generation

        active = self._get_active_generation(cur, space_key=space_key, for_update=True)
        if active is not None:
            self._assert_matching_profile(active, profile)
            return active

        cur.execute("SELECT EXISTS (SELECT 1 FROM chunks WHERE space_key = %s)", (space_key,))
        row = cur.fetchone()
        populated = bool(row and row[0])
        if populated or not allow_empty_bootstrap:
            raise EmbeddingProfileMismatchError(
                "No active embedding generation is available for this space"
            )

        profile_id = self._ensure_profile(cur, profile)
        cur.execute(
            """
            INSERT INTO space_generation
                (space_key, profile_id, state, activated_at)
            VALUES (%s, %s, 'active', now())
            RETURNING generation_id
            """,
            (space_key, profile_id),
        )
        created = cur.fetchone()
        if created is None:
            raise RuntimeError("Failed to create the initial embedding generation")
        generation = self._get_generation(
            cur,
            space_key=space_key,
            generation_id=int(created[0]),
            for_update=True,
        )
        if generation is None:
            raise RuntimeError("Created embedding generation could not be read back")
        return generation

    def create_building_generation(self, *, space_key: str) -> Generation:
        """Create an inactive generation for an explicit clean rebuild."""
        profile = self._require_profile()
        with self._connect() as conn, conn.cursor() as cur:
            self._lock_space_for_update(cur, space_key)
            profile_id = self._ensure_profile(cur, profile)
            cur.execute(
                """
                INSERT INTO space_generation (space_key, profile_id, state)
                VALUES (%s, %s, 'building')
                RETURNING generation_id
                """,
                (space_key, profile_id),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("Failed to create building embedding generation")
            generation = self._get_generation(
                cur,
                space_key=space_key,
                generation_id=int(row[0]),
                for_update=True,
            )
            if generation is None:
                raise RuntimeError("Created embedding generation could not be read back")
            return generation

    def activate_generation(self, *, space_key: str, generation_id: int) -> Generation:
        """Atomically retire the current generation and activate a verified build."""
        profile = self._require_profile()
        with self._connect() as conn, conn.cursor() as cur:
            self._lock_space_for_update(cur, space_key)
            target = self._get_generation(
                cur,
                space_key=space_key,
                generation_id=generation_id,
                for_update=True,
            )
            if target is None or target.state is not GenerationState.BUILDING:
                raise EmbeddingProfileMismatchError(
                    "Only a building generation in this space can be activated"
                )
            self._assert_matching_profile(target, profile)
            cur.execute(
                """
                UPDATE space_generation
                   SET state = 'retired'
                 WHERE space_key = %s AND state = 'active'
                """,
                (space_key,),
            )
            cur.execute(
                """
                UPDATE space_generation
                   SET state = 'active', activated_at = now()
                 WHERE space_key = %s AND generation_id = %s AND state = 'building'
                """,
                (space_key, generation_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("Embedding generation activation did not update one row")
            activated = self._get_generation(
                cur,
                space_key=space_key,
                generation_id=generation_id,
                for_update=True,
            )
            if activated is None:
                raise RuntimeError("Activated embedding generation could not be read back")
            return activated

    @staticmethod
    def _validate_batch(chunks: list[Chunk], embeddings: list[list[float]]) -> None:
        if len(chunks) != len(embeddings):
            msg = f"chunks/embeddings length mismatch: {len(chunks)} != {len(embeddings)}"
            raise ValueError(msg)
        for emb in embeddings:
            if len(emb) != EMBEDDING_DIM:
                msg = f"embedding dimension {len(emb)} != {EMBEDDING_DIM}"
                raise ValueError(msg)

    @staticmethod
    def _insert_rows(
        cur: psycopg.Cursor,
        chunks: list[Chunk],
        embeddings: list[list[float]],
    ) -> None:
        for chunk, emb in zip(chunks, embeddings, strict=True):
            cur.execute(
                """
                INSERT INTO chunks
                    (space_key, chunk_id, source_id, title, heading, breadcrumb,
                     content, embedding, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (space_key, chunk_id) DO UPDATE SET
                    source_id = EXCLUDED.source_id,
                    title = EXCLUDED.title,
                    heading = EXCLUDED.heading,
                    breadcrumb = EXCLUDED.breadcrumb,
                    content = EXCLUDED.content,
                    embedding = EXCLUDED.embedding,
                    updated_at = now()
                """,
                (
                    chunk.space_key,
                    chunk.chunk_id,
                    chunk.source_id,
                    chunk.title,
                    chunk.heading,
                    chunk.breadcrumb,
                    chunk.content,
                    emb,
                ),
            )

    def search(
        self,
        query_embedding: list[float],
        *,
        space_key: str | None = None,
        top_k: int = 5,
    ) -> list[SearchHit]:
        """Cosine-distance search, optionally isolated to one space."""
        if len(query_embedding) != EMBEDDING_DIM:
            msg = f"query embedding dimension {len(query_embedding)} != {EMBEDDING_DIM}"
            raise ValueError(msg)
        if top_k < 1:
            msg = f"top_k must be >= 1, got {top_k}"
            raise ValueError(msg)
        where = "WHERE space_key = %(space)s" if space_key else ""
        sql = f"""
            SELECT chunk_id, source_id, space_key, title, heading, breadcrumb, content,
                   embedding <=> %(query)s::vector AS distance
            FROM chunks
            {where}
            ORDER BY distance
            LIMIT %(k)s
        """
        params: dict[str, object] = {"query": query_embedding, "k": top_k}
        if space_key:
            params["space"] = space_key
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            SearchHit(
                chunk_id=row[0],
                source_id=row[1],
                space_key=row[2],
                title=row[3],
                heading=row[4],
                breadcrumb=row[5],
                content=row[6],
                distance=float(row[7]),
            )
            for row in rows
        ]

    def search_hybrid(
        self,
        query_embedding: list[float],
        query_text: str,
        *,
        space_key: str | None = None,
        top_k: int = 5,
        candidate_k: int = 20,
        rrf_k: int = 60,
        trgm_threshold: float = 0.2,
    ) -> list[SearchHit]:
        """Fuse vector, English FTS, and trigram candidates with reciprocal rank fusion.

        The trigram list (``pg_trgm`` ``word_similarity``) supplies language-agnostic
        substring recall — notably Korean, which the english FTS path handles poorly.
        Like the FTS path, it degrades gracefully to an empty list when nothing clears
        ``trgm_threshold``; RRF simply fuses whichever sources produced candidates.
        """
        if len(query_embedding) != EMBEDDING_DIM:
            msg = f"query embedding dimension {len(query_embedding)} != {EMBEDDING_DIM}"
            raise ValueError(msg)
        if top_k < 1:
            msg = f"top_k must be >= 1, got {top_k}"
            raise ValueError(msg)
        if candidate_k < top_k:
            msg = f"candidate_k must be >= top_k, got {candidate_k} < {top_k}"
            raise ValueError(msg)
        if rrf_k <= 0:
            msg = f"rrf_k must be > 0, got {rrf_k}"
            raise ValueError(msg)
        if not 0.0 <= trgm_threshold <= 1.0:
            msg = f"trgm_threshold must be in [0, 1], got {trgm_threshold}"
            raise ValueError(msg)

        space_filter = "AND space_key = %(space)s" if space_key else ""
        # Candidate rank feeds straight into RRF scoring, so tie order must be
        # deterministic — Postgres does not guarantee equal-score row order, and a
        # tie at the candidate_k boundary could otherwise flip which chunks (and
        # citations) surface between runs. space_key + chunk_id (the composite PK)
        # breaks every tie stably.
        vector_sql = f"""
            SELECT chunk_id, source_id, space_key, title, heading, breadcrumb, content,
                   embedding <=> %(query)s::vector AS distance
            FROM chunks
            WHERE TRUE {space_filter}
            ORDER BY distance, space_key, chunk_id
            LIMIT %(candidate_k)s
        """
        fts_sql = f"""
            SELECT chunk_id, source_id, space_key, title, heading, breadcrumb, content,
                   embedding <=> %(query)s::vector AS distance,
                   ts_rank_cd(
                       content_tsv, websearch_to_tsquery('english', %(query_text)s)
                   ) AS text_score
            FROM chunks
            WHERE content_tsv @@ websearch_to_tsquery('english', %(query_text)s)
                  {space_filter}
            ORDER BY text_score DESC, space_key, chunk_id
            LIMIT %(candidate_k)s
        """
        # word_similarity(query, content): the query's trigrams matched against the most
        # similar contiguous extent of content — the right metric for a short query
        # against a long chunk. The `<%%` operator (escaped `%` for psycopg) uses the
        # session word_similarity_threshold, letting the GIN index prune and the source
        # degrade to empty when nothing is similar enough.
        trgm_sql = f"""
            SELECT chunk_id, source_id, space_key, title, heading, breadcrumb, content,
                   embedding <=> %(query)s::vector AS distance,
                   word_similarity(%(query_text)s, content) AS trgm_score
            FROM chunks
            WHERE %(query_text)s <%% content
                  {space_filter}
            ORDER BY trgm_score DESC, space_key, chunk_id
            LIMIT %(candidate_k)s
        """
        params: dict[str, object] = {
            "query": query_embedding,
            "query_text": query_text,
            "candidate_k": candidate_k,
        }
        if space_key:
            params["space"] = space_key

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(vector_sql, params)
            vector_rows = cur.fetchall()
            cur.execute(fts_sql, params)
            fts_rows = cur.fetchall()
            # SET does not accept bind parameters; set_config() does (text value).
            # is_local=true scopes the GUC to this transaction so it cannot leak to a
            # later query if the connection is ever pooled.
            cur.execute(
                "SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)",
                (str(trgm_threshold),),
            )
            cur.execute(trgm_sql, params)
            trgm_rows = cur.fetchall()

        # chunk_id is only unique within a space (the table has a composite PK).
        # Keep both parts internally so unfiltered searches do not collapse rows.
        def key_for(row: tuple[Any, ...]) -> tuple[str, str]:
            return str(row[2]), str(row[0])

        rows_by_key = {key_for(row): row for row in vector_rows}
        rows_by_key.update({key_for(row): row for row in fts_rows})
        rows_by_key.update({key_for(row): row for row in trgm_rows})
        vector_ranks = {
            key_for(row): rank for rank, row in enumerate(vector_rows, start=1)
        }
        fts_ranks = {key_for(row): rank for rank, row in enumerate(fts_rows, start=1)}
        trgm_ranks = {key_for(row): rank for rank, row in enumerate(trgm_rows, start=1)}

        scored: list[tuple[float, tuple[str, str]]] = []
        for candidate_key in rows_by_key:
            score = 0.0
            if candidate_key in vector_ranks:
                score += 1.0 / (rrf_k + vector_ranks[candidate_key])
            if candidate_key in fts_ranks:
                score += 1.0 / (rrf_k + fts_ranks[candidate_key])
            if candidate_key in trgm_ranks:
                score += 1.0 / (rrf_k + trgm_ranks[candidate_key])
            scored.append((score, candidate_key))
        scored.sort(
            key=lambda item: (
                -item[0],
                float(rows_by_key[item[1]][7]),
                item[1],
            )
        )

        hits: list[SearchHit] = []
        for score, candidate_key in scored[:top_k]:
            row = rows_by_key[candidate_key]
            hits.append(
                SearchHit(
                    chunk_id=row[0],
                    source_id=row[1],
                    space_key=row[2],
                    title=row[3],
                    heading=row[4],
                    breadcrumb=row[5],
                    content=row[6],
                    distance=float(row[7]),
                    rrf_score=score,
                    fts_rank=fts_ranks.get(candidate_key),
                    trgm_rank=trgm_ranks.get(candidate_key),
                )
            )
        return hits
