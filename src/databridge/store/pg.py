"""pgvector store: vector and RRF hybrid search with metadata filters.

Hybrid full-text search uses PostgreSQL's stock ``english`` configuration. It improves
English keyword retrieval but does not provide Korean-aware tokenization or stemming;
that known limitation is deliberately scoped out of this phase. Space isolation is a
plain ``WHERE space_key = %s``.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Any

import psycopg
from pgvector.psycopg import register_vector

from databridge.embed.base import EMBEDDING_DIM
from databridge.ingest.chunker import Chunk


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


class PgVectorStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

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
    ) -> list[SearchHit]:
        """Fuse vector and English full-text candidates with reciprocal rank fusion."""
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

        space_filter = "AND space_key = %(space)s" if space_key else ""
        vector_sql = f"""
            SELECT chunk_id, source_id, space_key, title, heading, breadcrumb, content,
                   embedding <=> %(query)s::vector AS distance
            FROM chunks
            WHERE TRUE {space_filter}
            ORDER BY distance
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
            ORDER BY text_score DESC
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

        # chunk_id is only unique within a space (the table has a composite PK).
        # Keep both parts internally so unfiltered searches do not collapse rows.
        def key_for(row: tuple[Any, ...]) -> tuple[str, str]:
            return str(row[2]), str(row[0])

        rows_by_key = {key_for(row): row for row in vector_rows}
        rows_by_key.update({key_for(row): row for row in fts_rows})
        vector_ranks = {
            key_for(row): rank for rank, row in enumerate(vector_rows, start=1)
        }
        fts_ranks = {key_for(row): rank for rank, row in enumerate(fts_rows, start=1)}

        scored: list[tuple[float, tuple[str, str]]] = []
        for candidate_key in rows_by_key:
            score = 0.0
            if candidate_key in vector_ranks:
                score += 1.0 / (rrf_k + vector_ranks[candidate_key])
            if candidate_key in fts_ranks:
                score += 1.0 / (rrf_k + fts_ranks[candidate_key])
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
                )
            )
        return hits
