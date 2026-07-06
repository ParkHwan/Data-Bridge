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


def _replace(store: PgVectorStore, embedder: HashedEmbedder, doc: SourceDocument) -> int:
    chunks = chunk_document(doc)
    return store.replace_source(
        space_key=doc.space_key,
        source_id=doc.source_id,
        chunks=chunks,
        embeddings=embedder.embed([c.embedding_text for c in chunks]),
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
        _replace(store, embedder, doc)

    query = embedder.embed(["how do I rollback a release"])[0]

    hits_a = store.search(query, space_key="SPACE_A", top_k=2)
    assert hits_a and hits_a[0].source_id == "t-rollback"
    assert all(h.space_key == "SPACE_A" for h in hits_a)

    # Unfiltered search spans spaces. The shared dev DB may hold other corpora
    # (e.g. DEMO samples), so assert superset membership, not equality.
    hits_all = store.search(query, top_k=50)
    assert {"SPACE_A", "SPACE_B"} <= {h.space_key for h in hits_all}

    # atomic replace is idempotent — row count stays stable
    count = _replace(store, embedder, docs[0])
    hits_again = store.search(query, space_key="SPACE_A", top_k=5)
    assert len([h for h in hits_again if h.source_id == "t-rollback"]) == count


def test_same_source_id_in_two_spaces_do_not_clobber() -> None:
    """Post-review P1: mutations honor space isolation (composite PK)."""
    store = PgVectorStore(DSN)
    store.ensure_schema()
    embedder = HashedEmbedder()

    doc_a = _doc("t-shared", "ISO_A", "alpha content about deployment")
    doc_b = _doc("t-shared", "ISO_B", "beta content about pricing")
    _replace(store, embedder, doc_a)
    _replace(store, embedder, doc_b)

    q = embedder.embed(["deployment"])[0]
    hits_a = store.search(q, space_key="ISO_A", top_k=5)
    hits_b = store.search(q, space_key="ISO_B", top_k=5)
    assert {h.source_id for h in hits_a} == {"t-shared"}
    assert {h.source_id for h in hits_b} == {"t-shared"}
    assert hits_a[0].content != hits_b[0].content

    # scoped delete removes only one space's copy
    deleted = store.delete_source(space_key="ISO_A", source_id="t-shared")
    assert deleted > 0
    assert store.search(q, space_key="ISO_A", top_k=5) == []
    assert store.search(q, space_key="ISO_B", top_k=5) != []


def test_search_validates_inputs() -> None:
    store = PgVectorStore(DSN)
    store.ensure_schema()
    with pytest.raises(ValueError, match="dimension"):
        store.search([0.0] * 3, top_k=1)
    with pytest.raises(ValueError, match="top_k"):
        store.search([0.0] * 768, top_k=0)
