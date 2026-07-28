"""Integration test — requires the docker compose Postgres (marker: integration)."""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import psycopg
import pytest

from databridge.embed import EmbeddingProfile, HashedEmbedder
from databridge.embed.base import make_config_fingerprint
from databridge.ingest.chunker import Chunk, chunk_document
from databridge.ingest.markdown import SourceDocument
from databridge.store import (
    EmbeddingProfileMismatchError,
    GenerationState,
    PgVectorStore,
    ProfileMode,
)

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


def _store(
    *,
    profile: EmbeddingProfile | None = None,
    mode: ProfileMode = ProfileMode.STRICT,
) -> PgVectorStore:
    return PgVectorStore(
        DSN,
        profile=profile or HashedEmbedder().profile,
        mode=mode,
    )


def _other_profile() -> EmbeddingProfile:
    provider = "hashed"
    model = "hashed-test-alternate"
    dimension = 768
    return EmbeddingProfile(
        provider=provider,
        model=model,
        dimension=dimension,
        config_fingerprint=make_config_fingerprint(
            provider=provider,
            model=model,
            dimension=dimension,
            options={"variant": "alternate"},
        ),
    )


def _unique_space(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _insert_legacy_chunk(space: str, source_id: str = "legacy") -> None:
    vector = "[" + ",".join(["0"] * 768) + "]"
    with psycopg.connect(DSN) as conn:
        conn.execute(
            """
            INSERT INTO chunks
                (space_key, generation_id, chunk_id, source_id, title, content, embedding)
            VALUES (%s, NULL, %s, %s, %s, %s, %s::vector)
            """,
            (space, f"{source_id}#0", source_id, source_id, "legacy body", vector),
        )


def _replace(
    store: PgVectorStore,
    embedder: HashedEmbedder,
    doc: SourceDocument,
    *,
    generation_id: int | None = None,
) -> int:
    chunks = chunk_document(doc)
    return store.replace_source(
        space_key=doc.space_key,
        source_id=doc.source_id,
        chunks=chunks,
        embeddings=embedder.embed([c.embedding_text for c in chunks]),
        generation_id=generation_id,
    )


def test_upsert_search_and_space_isolation() -> None:
    store = _store()
    store.ensure_schema()
    embedder = HashedEmbedder()
    space_a = _unique_space("SPACE_A")
    space_b = _unique_space("SPACE_B")

    docs = [
        _doc("t-rollback", space_a, "rollback procedure error rate release profile"),
        _doc("t-pricing", space_a, "pricing per seat usage events enterprise fee"),
        _doc("t-other", space_b, "rollback procedure in another space"),
    ]
    for doc in docs:
        _replace(store, embedder, doc)

    query = embedder.embed(["how do I rollback a release"])[0]

    hits_a = store.search(query, space_key=space_a, top_k=2)
    assert hits_a and hits_a[0].source_id == "t-rollback"
    assert all(h.space_key == space_a for h in hits_a)

    # atomic replace is idempotent — row count stays stable
    count = _replace(store, embedder, docs[0])
    hits_again = store.search(query, space_key=space_a, top_k=5)
    assert len([h for h in hits_again if h.source_id == "t-rollback"]) == count


def test_same_source_id_in_two_spaces_do_not_clobber() -> None:
    """Post-review P1: mutations honor space isolation (composite PK)."""
    store = _store()
    store.ensure_schema()
    embedder = HashedEmbedder()
    space_a = _unique_space("ISO_A")
    space_b = _unique_space("ISO_B")

    doc_a = _doc("t-shared", space_a, "alpha content about deployment")
    doc_b = _doc("t-shared", space_b, "beta content about pricing")
    _replace(store, embedder, doc_a)
    _replace(store, embedder, doc_b)

    q = embedder.embed(["deployment"])[0]
    hits_a = store.search(q, space_key=space_a, top_k=5)
    hits_b = store.search(q, space_key=space_b, top_k=5)
    assert {h.source_id for h in hits_a} == {"t-shared"}
    assert {h.source_id for h in hits_b} == {"t-shared"}
    assert hits_a[0].content != hits_b[0].content

    # scoped delete removes only one space's copy
    deleted = store.delete_source(space_key=space_a, source_id="t-shared")
    assert deleted > 0
    assert store.search(q, space_key=space_a, top_k=5) == []
    assert store.search(q, space_key=space_b, top_k=5) != []


def test_list_source_ids_and_advisory_lock_are_space_scoped() -> None:
    store = _store()
    store.ensure_schema()
    embedder = HashedEmbedder()
    space_a = _unique_space("LIST_A")
    space_b = _unique_space("LIST_B")
    lock_key = _unique_space("test-list-lock")
    _replace(store, embedder, _doc("list-a", space_a, "alpha"))
    _replace(store, embedder, _doc("list-b", space_a, "beta"))
    _replace(store, embedder, _doc("list-other", space_b, "gamma"))

    assert store.list_source_ids(space_key=space_a) == {"list-a", "list-b"}
    with store.advisory_lock(lock_key) as first:
        assert first is True
        with store.advisory_lock(lock_key) as second:
            assert second is False
    with store.advisory_lock(lock_key) as reacquired:
        assert reacquired is True


def test_search_validates_inputs() -> None:
    store = _store()
    store.ensure_schema()
    space = _unique_space("VALIDATION")
    with pytest.raises(ValueError, match="dimension"):
        store.search([0.0] * 3, space_key=space, top_k=1)
    with pytest.raises(ValueError, match="top_k"):
        store.search([0.0] * 768, space_key=space, top_k=0)


def test_hybrid_search_fuses_signals_and_honors_top_k() -> None:
    store = _store()
    store.ensure_schema()
    space = _unique_space("HYBRID_FUSION_TEST")
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
    store = _store()
    store.ensure_schema()
    embedder = HashedEmbedder()
    space_a = _unique_space("HYBRID_ISO_A")
    space_b = _unique_space("HYBRID_ISO_B")
    doc_a = _doc("hybrid-a", space_a, "alpha deployment procedure")
    doc_b = _doc("hybrid-b", space_b, "beta pricing policy")
    _replace(store, embedder, doc_a)
    _replace(store, embedder, doc_b)
    query = embedder.embed(["!!!"])[0]

    hits = store.search_hybrid(
        query, "!!!", space_key=space_a, top_k=1, candidate_k=2
    )
    assert len(hits) == 1
    assert hits[0].source_id == "hybrid-a"
    assert hits[0].fts_rank is None


def test_hybrid_search_validates_inputs() -> None:
    store = _store()
    store.ensure_schema()
    embedding = [0.0] * 768
    space = _unique_space("VALIDATION")
    with pytest.raises(ValueError, match="dimension"):
        store.search_hybrid([0.0] * 3, "query", space_key=space)
    with pytest.raises(ValueError, match="top_k"):
        store.search_hybrid(embedding, "query", space_key=space, top_k=0)
    with pytest.raises(ValueError, match="candidate_k"):
        store.search_hybrid(
            embedding, "query", space_key=space, top_k=5, candidate_k=4
        )
    with pytest.raises(ValueError, match="rrf_k"):
        store.search_hybrid(embedding, "query", space_key=space, rrf_k=0)
    with pytest.raises(ValueError, match="trgm_threshold"):
        store.search_hybrid(
            embedding, "query", space_key=space, trgm_threshold=1.5
        )


def test_hybrid_korean_josa_recall_via_trigram() -> None:
    """Trigram recovers a Korean chunk that english FTS misses on josa variants.

    ``배포를``/``완료했다`` tokenize as-is under the ``english`` config, so the bare query
    terms ``배포``/``완료`` share no token with the chunk — english FTS finds nothing.
    Character trigrams match the substrings, so the trigram source recovers the chunk.
    """
    store = _store()
    store.ensure_schema()
    embedder = HashedEmbedder()
    space = _unique_space("KO_TRGM_TEST")
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
    store = _store()
    store.ensure_schema()
    space = _unique_space("TRI_FUSION_TEST")
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


def test_profile_and_generation_survive_last_source_deletion() -> None:
    store = _store()
    store.ensure_schema()
    space = _unique_space("PRESERVE")
    _replace(store, HashedEmbedder(), _doc("only", space, "one source"))
    before = store.profile_report(space_key=space)

    assert store.delete_source(space_key=space, source_id="only") > 0
    after = store.profile_report(space_key=space)

    assert after.active_generation_id == before.active_generation_id
    assert after.distinct_profile_count == 1
    assert sum(item.chunk_count for item in after.generation_chunk_counts) == 0


def test_strict_mismatch_rejects_read_and_preflight_before_query() -> None:
    writer = _store()
    writer.ensure_schema()
    space = _unique_space("STRICT_MISMATCH")
    embedder = HashedEmbedder()
    _replace(writer, embedder, _doc("source", space, "retained evidence"))
    incompatible = _store(profile=_other_profile(), mode=ProfileMode.STRICT)

    with pytest.raises(EmbeddingProfileMismatchError):
        incompatible.preflight(space_key=space)
    with pytest.raises(EmbeddingProfileMismatchError):
        incompatible.search(embedder.embed(["evidence"])[0], space_key=space)


def test_observe_mismatch_reads_active_generation_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer = _store()
    writer.ensure_schema()
    space = _unique_space("OBSERVE_MISMATCH")
    embedder = HashedEmbedder()
    _replace(writer, embedder, _doc("source", space, "observable evidence"))
    observer = _store(profile=_other_profile(), mode=ProfileMode.OBSERVE)

    with caplog.at_level("WARNING"):
        observer.preflight(space_key=space)
        hits = observer.search(embedder.embed(["evidence"])[0], space_key=space)

    assert hits
    assert "profile mismatch observed" in caplog.text


def test_populated_legacy_space_is_not_automatically_labelled() -> None:
    store = _store()
    store.ensure_schema()
    space = _unique_space("LEGACY_REJECT")
    _insert_legacy_chunk(space)

    with pytest.raises(EmbeddingProfileMismatchError, match="No active"):
        _replace(store, HashedEmbedder(), _doc("new", space, "new body"))

    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT count(*) FROM chunks WHERE space_key = %s AND generation_id IS NULL",
            (space,),
        ).fetchone()
    assert row == (1,)


def test_legacy_null_chunks_are_not_active_in_either_mode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strict = _store()
    strict.ensure_schema()
    space = _unique_space("LEGACY_READ")
    _insert_legacy_chunk(space)
    query = HashedEmbedder().embed(["legacy"])[0]

    with pytest.raises(EmbeddingProfileMismatchError):
        strict.search(query, space_key=space)
    observe = _store(mode=ProfileMode.OBSERVE)
    with caplog.at_level("WARNING"):
        assert observe.search(query, space_key=space) == []
    assert "No active embedding generation" in caplog.text


def test_generation_build_is_isolated_until_atomic_activation() -> None:
    store = _store()
    store.ensure_schema()
    embedder = HashedEmbedder()
    space = _unique_space("ACTIVATE")
    _replace(store, embedder, _doc("shared", space, "old active content"))
    old_active = store.profile_report(space_key=space).active_generation_id
    building = store.create_building_generation(space_key=space)
    _replace(
        store,
        embedder,
        _doc("shared", space, "new building content"),
        generation_id=building.generation_id,
    )

    assert store.list_source_ids(space_key=space) == {"shared"}
    assert store.list_source_ids(
        space_key=space, generation_id=building.generation_id
    ) == {"shared"}
    assert "old active" in store.search(
        embedder.embed(["content"])[0], space_key=space
    )[0].content

    activated = store.activate_generation(
        space_key=space, generation_id=building.generation_id
    )
    report = store.profile_report(space_key=space)
    assert activated.state is GenerationState.ACTIVE
    assert report.active_generation_id == building.generation_id
    assert report.active_generation_id != old_active
    assert "new building" in store.search(
        embedder.embed(["content"])[0], space_key=space
    )[0].content


def test_active_and_building_gc_are_source_isolated() -> None:
    store = _store()
    store.ensure_schema()
    embedder = HashedEmbedder()
    space = _unique_space("GEN_GC")
    _replace(store, embedder, _doc("active-only", space, "active"))
    building = store.create_building_generation(space_key=space)
    _replace(
        store,
        embedder,
        _doc("build-only", space, "building"),
        generation_id=building.generation_id,
    )

    assert store.delete_source(
        space_key=space,
        source_id="build-only",
        generation_id=building.generation_id,
    ) > 0
    assert store.list_source_ids(space_key=space) == {"active-only"}
    assert store.list_source_ids(
        space_key=space, generation_id=building.generation_id
    ) == set()


def test_failed_activation_preserves_existing_active_pointer() -> None:
    store = _store()
    store.ensure_schema()
    space = _unique_space("FAILED_ACTIVATE")
    _replace(store, HashedEmbedder(), _doc("active", space, "active"))
    active_before = store.profile_report(space_key=space).active_generation_id

    with pytest.raises(EmbeddingProfileMismatchError):
        store.activate_generation(space_key=space, generation_id=9_223_372_036_854_775_000)

    assert store.profile_report(space_key=space).active_generation_id == active_before


def test_profile_mismatch_fails_before_replace_delete() -> None:
    writer = _store()
    writer.ensure_schema()
    embedder = HashedEmbedder()
    space = _unique_space("PREDELETE")
    _replace(writer, embedder, _doc("source", space, "must survive"))
    incompatible = _store(profile=_other_profile())

    with pytest.raises(EmbeddingProfileMismatchError):
        _replace(incompatible, embedder, _doc("source", space, "must not replace"))

    hits = writer.search(embedder.embed(["survive"])[0], space_key=space)
    assert hits and "must survive" in hits[0].content


def test_concurrent_initial_writers_share_one_active_generation() -> None:
    schema_store = _store()
    schema_store.ensure_schema()
    embedder = HashedEmbedder()
    space = _unique_space("FIRST_WRITER")
    barrier = threading.Barrier(2)

    def write(source_id: str) -> int:
        barrier.wait()
        return _replace(_store(), embedder, _doc(source_id, space, source_id))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["one", "two"]))

    report = schema_store.profile_report(space_key=space)
    assert results == [1, 1]
    assert report.active_generation_id is not None
    active_counts = [
        item for item in report.generation_chunk_counts if item.state is GenerationState.ACTIVE
    ]
    assert len(active_counts) == 1
    assert schema_store.list_source_ids(space_key=space) == {"one", "two"}


def test_writer_activation_race_is_serialized() -> None:
    store = _store()
    store.ensure_schema()
    embedder = HashedEmbedder()
    space = _unique_space("ACTIVATION_RACE")
    _replace(store, embedder, _doc("old", space, "old"))
    building = store.create_building_generation(space_key=space)
    barrier = threading.Barrier(2)

    def write() -> str:
        barrier.wait()
        try:
            _replace(
                _store(),
                embedder,
                _doc("racing", space, "racing"),
                generation_id=building.generation_id,
            )
        except EmbeddingProfileMismatchError:
            return "activation-won"
        return "writer-won"

    def activate() -> int:
        barrier.wait()
        return _store().activate_generation(
            space_key=space, generation_id=building.generation_id
        ).generation_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        write_future = pool.submit(write)
        activate_future = pool.submit(activate)
        write_outcome = write_future.result()
        activated_id = activate_future.result()

    assert write_outcome in {"writer-won", "activation-won"}
    assert activated_id == building.generation_id
    assert store.profile_report(space_key=space).active_generation_id == building.generation_id


def test_existing_legacy_schema_migrates_idempotently_without_backfill() -> None:
    schema = f"migration_{uuid.uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'SET search_path TO "{schema}", public')
        conn.execute(
            """
            CREATE TABLE chunks (
                space_key TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                title TEXT NOT NULL,
                heading TEXT,
                breadcrumb TEXT,
                content TEXT NOT NULL,
                embedding vector(768) NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (space_key, chunk_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO chunks
                (space_key, chunk_id, source_id, title, content, embedding)
            VALUES ('LEGACY', 'legacy#0', 'legacy', 'legacy', 'body',
                    ('[' || repeat('0,', 767) || '0]')::vector)
            """
        )
    separator = "&" if "?" in DSN else "?"
    scoped_dsn = f"{DSN}{separator}options={quote(f'-csearch_path={schema},public')}"
    migration_store = PgVectorStore.for_migration(scoped_dsn)
    try:
        migration_store.ensure_schema()
        migration_store.ensure_schema()
        with psycopg.connect(scoped_dsn) as conn:
            row = conn.execute(
                "SELECT generation_id, id IS NOT NULL FROM chunks WHERE chunk_id='legacy#0'"
            ).fetchone()
        assert row == (None, True)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
