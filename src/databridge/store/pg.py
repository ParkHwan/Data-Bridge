"""pgvector store: upsert + vector-similarity search with metadata filter.

MVP search scope is deliberately vector + filter only (design D-11); RRF hybrid is a
Phase-3 option. Space isolation is a plain ``WHERE space_key = %s`` — the reason this
store replaces the sibling's source-prefix workaround.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

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

    def upsert_chunks(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int:
        if len(chunks) != len(embeddings):
            msg = f"chunks/embeddings length mismatch: {len(chunks)} != {len(embeddings)}"
            raise ValueError(msg)
        for emb in embeddings:
            if len(emb) != EMBEDDING_DIM:
                msg = f"embedding dimension {len(emb)} != {EMBEDDING_DIM}"
                raise ValueError(msg)
        with self._connect() as conn, conn.cursor() as cur:
            for chunk, emb in zip(chunks, embeddings, strict=True):
                cur.execute(
                    """
                    INSERT INTO chunks
                        (chunk_id, source_id, space_key, title, heading, breadcrumb,
                         content, embedding, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        source_id = EXCLUDED.source_id,
                        space_key = EXCLUDED.space_key,
                        title = EXCLUDED.title,
                        heading = EXCLUDED.heading,
                        breadcrumb = EXCLUDED.breadcrumb,
                        content = EXCLUDED.content,
                        embedding = EXCLUDED.embedding,
                        updated_at = now()
                    """,
                    (
                        chunk.chunk_id,
                        chunk.source_id,
                        chunk.space_key,
                        chunk.title,
                        chunk.heading,
                        chunk.breadcrumb,
                        chunk.content,
                        emb,
                    ),
                )
        return len(chunks)

    def delete_source(self, source_id: str) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM chunks WHERE source_id = %s", (source_id,))
            return cur.rowcount or 0

    def search(
        self,
        query_embedding: list[float],
        *,
        space_key: str | None = None,
        top_k: int = 5,
    ) -> list[SearchHit]:
        """Cosine-distance search, optionally isolated to one space."""
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
