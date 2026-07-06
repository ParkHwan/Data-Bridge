"""Integration test — requires the docker compose Postgres (marker: integration)."""

from __future__ import annotations

import os

import psycopg
import pytest

from databridge.embed import HashedEmbedder
from databridge.ingest.chunker import chunk_document
from databridge.ingest.markdown import SourceDocument
from databridge.store import PgVectorStore

DSN = os.environ.get("DATABRIDGE_DSN", "postgresql://databridge:databridge@localhost:5433/databridge")


def _pg_available() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _pg_available(), reason="postgres not reachable (docker compose up -d)"),
]


def _doc(source_id: str, space: str, text: str) -> SourceDocument:
    return SourceDocument(
        source_id=source_id, title=source_id, space_key=space, body=f"## S\n{text}"
    )


def test_upsert_search_and_space_isolation() -> None:
    store = PgVectorStore(DSN)
    store.ensure_schema()
    embedder = HashedEmbedder()

    docs = [
        _doc("t-rollback", "SPACE_A", "rollback procedure error rate release profile"),
        _doc("t-pricing", "SPACE_A", "pricing per seat usage events enterprise fee"),
        _doc("t-other", "SPACE_B", "rollback procedure in another space"),
    ]
    for doc in docs:
        chunks = chunk_document(doc)
        store.delete_source(doc.source_id)
        store.upsert_chunks(chunks, embedder.embed([c.embedding_text for c in chunks]))

    query = embedder.embed(["how do I rollback a release"])[0]

    hits_a = store.search(query, space_key="SPACE_A", top_k=2)
    assert hits_a and hits_a[0].source_id == "t-rollback"
    assert all(h.space_key == "SPACE_A" for h in hits_a)

    hits_all = store.search(query, top_k=3)
    assert {h.space_key for h in hits_all} == {"SPACE_A", "SPACE_B"}

    # idempotent re-upsert keeps row count stable
    doc = docs[0]
    chunks = chunk_document(doc)
    store.upsert_chunks(chunks, embedder.embed([c.embedding_text for c in chunks]))
    hits_again = store.search(query, space_key="SPACE_A", top_k=5)
    assert len([h for h in hits_again if h.source_id == "t-rollback"]) == len(chunks)
