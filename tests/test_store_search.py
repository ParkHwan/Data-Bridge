"""Integration test — requires the docker compose Postgres (marker: integration)."""

from __future__ import annotations

import os

import psycopg
import pytest

from databridge.embed import HashedEmbedder
from databridge.ingest.chunker import Chunk, chunk_document
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


def test_hybrid_search_fuses_signals_and_honors_top_k() -> None:
    store = PgVectorStore(DSN)
    store.ensure_schema()
    space = "HYBRID_FUSION_TEST"
    chunks = [
        Chunk("both#0", "both", space, "both", "S", None, "exactterm", 0),
        Chunk("vector#0", "vector", space, "vector", "S", None, "unrelated", 0),
        Chunk("fts#0", "fts", space, "fts", "S", None, "exactterm", 0),
    ]
    query = [1.0] + [0.0] * 767
    store.replace_source(
        space_key=space,
        source_id="both",
        chunks=[chunks[0]],
        embeddings=[query],
    )
    store.replace_source(
        space_key=space,
        source_id="vector",
        chunks=[chunks[1]],
        embeddings=[query],
    )
    store.replace_source(
        space_key=space,
        source_id="fts",
        chunks=[chunks[2]],
        embeddings=[[-1.0] + [0.0] * 767],
    )

    hits = store.search_hybrid(
        query, "exactterm", space_key=space, top_k=2, candidate_k=2
    )
    assert len(hits) == 2
    assert hits[0].source_id == "both"
    assert hits[0].fts_rank is not None
    assert hits[0].rrf_score is not None


def test_hybrid_search_space_isolation_and_vector_only_degradation() -> None:
    store = PgVectorStore(DSN)
    store.ensure_schema()
    embedder = HashedEmbedder()
    doc_a = _doc("hybrid-a", "HYBRID_ISO_A", "alpha deployment procedure")
    doc_b = _doc("hybrid-b", "HYBRID_ISO_B", "beta pricing policy")
    _replace(store, embedder, doc_a)
    _replace(store, embedder, doc_b)
    query = embedder.embed(["!!!"])[0]

    hits = store.search_hybrid(
        query, "!!!", space_key="HYBRID_ISO_A", top_k=1, candidate_k=2
    )
    assert len(hits) == 1
    assert hits[0].source_id == "hybrid-a"
    assert hits[0].fts_rank is None


def test_hybrid_search_validates_inputs() -> None:
    store = PgVectorStore(DSN)
    store.ensure_schema()
    embedding = [0.0] * 768
    with pytest.raises(ValueError, match="dimension"):
        store.search_hybrid([0.0] * 3, "query")
    with pytest.raises(ValueError, match="top_k"):
        store.search_hybrid(embedding, "query", top_k=0)
    with pytest.raises(ValueError, match="candidate_k"):
        store.search_hybrid(embedding, "query", top_k=5, candidate_k=4)
    with pytest.raises(ValueError, match="rrf_k"):
        store.search_hybrid(embedding, "query", rrf_k=0)
    with pytest.raises(ValueError, match="trgm_threshold"):
        store.search_hybrid(embedding, "query", trgm_threshold=1.5)


def test_hybrid_korean_josa_recall_via_trigram() -> None:
    """Trigram recovers a Korean chunk that english FTS misses on josa variants.

    ``배포를``/``완료했다`` tokenize as-is under the ``english`` config, so the bare query
    terms ``배포``/``완료`` share no token with the chunk — english FTS finds nothing.
    Character trigrams match the substrings, so the trigram source recovers the chunk.
    """
    store = PgVectorStore(DSN)
    store.ensure_schema()
    embedder = HashedEmbedder()
    space = "KO_TRGM_TEST"
    doc = SourceDocument(
        source_id="ko-deploy",
        title="ko-deploy",
        space_key=space,
        body="## S\n배포를 진행했다. 롤백 없이 안정적으로 완료했다.",
    )
    _replace(store, embedder, doc)

    query_emb = embedder.embed(["배포 완료"])[0]
    hits = store.search_hybrid(
        query_emb, "배포 완료", space_key=space, top_k=5, candidate_k=10
    )

    match = [h for h in hits if h.source_id == "ko-deploy"]
    assert match, "trigram should recover the Korean chunk"
    assert match[0].trgm_rank is not None, "trigram source should have matched"
    assert match[0].fts_rank is None, "english FTS should miss the josa-suffixed terms"


def test_hybrid_three_source_fusion() -> None:
    """All three signals (vector, FTS, trigram) contribute independently."""
    store = PgVectorStore(DSN)
    store.ensure_schema()
    space = "TRI_FUSION_TEST"
    query = [1.0] + [0.0] * 767
    # kw: FTS + trigram hit on "exactterm", vector far (opposite embedding).
    store.replace_source(
        space_key=space,
        source_id="kw",
        chunks=[Chunk("kw#0", "kw", space, "kw", "S", None, "exactterm keyword", 0)],
        embeddings=[[-1.0] + [0.0] * 767],
    )
    # vec: vector hit only, unrelated text so neither FTS nor trigram match.
    store.replace_source(
        space_key=space,
        source_id="vec",
        chunks=[Chunk("vec#0", "vec", space, "vec", "S", None, "unrelated body", 0)],
        embeddings=[query],
    )

    hits = store.search_hybrid(query, "exactterm", space_key=space, top_k=2, candidate_k=5)
    by_id = {h.source_id: h for h in hits}
    assert {"kw", "vec"} <= set(by_id)
    assert by_id["kw"].fts_rank is not None
    assert by_id["kw"].trgm_rank is not None
    assert by_id["vec"].fts_rank is None
    assert by_id["vec"].trgm_rank is None
