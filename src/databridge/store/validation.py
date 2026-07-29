"""Activation-gating validation for an explicit, non-serving generation."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from databridge.embed.base import Embedder
from databridge.store.exceptions import (
    GenerationConcurrencyError,
    GenerationTargetNotFoundError,
    GenerationTargetStateMismatchError,
    SearchGenerationValidationError,
    StructuralGenerationValidationError,
    ValidationQueryConfigurationError,
    ValidationQueryShaMismatchError,
)
from databridge.store.pg import PgVectorStore
from databridge.store.provenance import GenerationState

_VALIDATOR_VERSION = "generation-validator-v1"
_QUERY_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class QueryCategory(StrEnum):
    SOURCE_COVERAGE = "source_coverage"
    HEADING_INTENT = "heading_intent"
    EN_PARAPHRASE = "en_paraphrase"
    EN_KEYWORD = "en_keyword"
    KO_MORPHOLOGY = "ko_morphology"


class ValidationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictStr
    question: StrictStr
    category: QueryCategory
    expected_source_id: StrictStr
    expected_heading: StrictStr | None = None
    critical: StrictBool = False

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _QUERY_ID.fullmatch(value):
            raise ValueError("id must match ^[A-Za-z0-9_-]+$")
        return value

    @field_validator("question", "expected_source_id")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("expected_heading")
    @classmethod
    def _nonempty_heading(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("expected_heading must not be empty")
        return stripped

    @model_validator(mode="after")
    def _heading_intent_requires_heading(self) -> ValidationQuery:
        if self.category is QueryCategory.HEADING_INTENT and self.expected_heading is None:
            raise ValueError("heading_intent requires expected_heading")
        return self


class ValidationQueryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_key: StrictStr
    queries: tuple[ValidationQuery, ...]

    @field_validator("space_key")
    @classmethod
    def _space_nonempty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("space_key must not be empty")
        return stripped

    @model_validator(mode="after")
    def _query_contract(self) -> ValidationQueryFile:
        ids = [item.id for item in self.queries]
        if len(ids) != len(set(ids)):
            raise ValueError("query ids must be unique")
        if len(self.queries) < 5:
            raise ValueError("at least five validation queries are required")
        categories = {item.category for item in self.queries}
        required = {
            QueryCategory.EN_PARAPHRASE,
            QueryCategory.EN_KEYWORD,
            QueryCategory.KO_MORPHOLOGY,
        }
        missing = sorted(item.value for item in required - categories)
        if missing:
            raise ValueError(f"missing required query categories: {', '.join(missing)}")
        return self


@dataclass(frozen=True, slots=True)
class ValidationHit:
    generation_id: int
    chunk_id: str
    source_id: str
    heading: str | None
    rank: int


@dataclass(frozen=True, slots=True)
class ManifestSnapshot:
    state: str
    revision: int
    source_counts: dict[str, int]
    total_chunks: int


@dataclass(frozen=True, slots=True)
class _T1Snapshot:
    manifest: ManifestSnapshot
    checksum: str
    chunk_count: int
    profile_id: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    generation_id: int
    checksum: str
    chunk_count: int
    manifest_revision: int
    query_count: int


def load_validation_queries(
    path: Path, *, space_key: str, expected_sha256: str | None = None
) -> tuple[ValidationQueryFile, str]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValidationQueryConfigurationError(f"Invalid validation query file: {exc}") from exc

    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None:
        if not _SHA256.fullmatch(expected_sha256):
            raise ValidationQueryShaMismatchError(
                "--expected-queries-sha256 must be exactly 64 hexadecimal characters"
            )
        if actual_sha256 != expected_sha256.lower():
            raise ValidationQueryShaMismatchError(
                "Validation query file SHA-256 does not match --expected-queries-sha256"
            )

    try:
        raw: Any = yaml.safe_load(payload.decode("utf-8"))
        query_file = ValidationQueryFile.model_validate(raw)
    except (UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        raise ValidationQueryConfigurationError(f"Invalid validation query file: {exc}") from exc
    if query_file.space_key != space_key:
        raise ValidationQueryConfigurationError(
            f"Query file space {query_file.space_key!r} does not match --space {space_key!r}"
        )
    return query_file, actual_sha256


def search_hybrid_in_generation(
    store: PgVectorStore,
    *,
    space_key: str,
    generation_id: int,
    query_embedding: list[float],
    query_text: str,
    top_k: int = 5,
    candidate_k: int = 20,
    rrf_k: int = 60,
    trgm_threshold: float = 0.2,
) -> list[ValidationHit]:
    """Use the production RRF body without resolving or serving the generation."""
    with store._connect() as conn, conn.cursor() as cur:
        hits = store._search_hybrid_in_generation(
            cur,
            space_key=space_key,
            generation_id=generation_id,
            query_embedding=query_embedding,
            query_text=query_text,
            top_k=top_k,
            candidate_k=candidate_k,
            rrf_k=rrf_k,
            trgm_threshold=trgm_threshold,
        )
    return [
        ValidationHit(
            generation_id=hit.generation_id,
            chunk_id=hit.chunk_id,
            source_id=hit.source_id,
            heading=hit.heading,
            rank=rank,
        )
        for rank, hit in enumerate(hits, start=1)
    ]


def validate_generation(
    store: PgVectorStore,
    embedder: Embedder,
    *,
    space_key: str,
    generation_id: int,
    queries_path: Path,
    expected_queries_sha256: str,
) -> ValidationResult:
    """Run T1, unlocked searches, then T2 with concurrency checks first."""
    query_file, query_file_sha256 = load_validation_queries(
        queries_path,
        space_key=space_key,
        expected_sha256=expected_queries_sha256,
    )
    snapshot = _validate_t1(
        store,
        space_key=space_key,
        generation_id=generation_id,
        query_file=query_file,
    )

    embeddings = embedder.embed([item.question for item in query_file.queries])
    if len(embeddings) != len(query_file.queries):
        raise RuntimeError("Embedder returned the wrong validation query count")
    failures: list[str] = []
    for item, embedding in zip(query_file.queries, embeddings, strict=True):
        hits = search_hybrid_in_generation(
            store,
            space_key=space_key,
            generation_id=generation_id,
            query_embedding=embedding,
            query_text=item.question,
            top_k=5,
            candidate_k=20,
            rrf_k=60,
            trgm_threshold=0.2,
        )
        failure = _query_failure(item, hits, generation_id=generation_id)
        if failure is not None:
            failures.append(f"{item.id}: {failure}")

    _validate_t2(
        store,
        space_key=space_key,
        generation_id=generation_id,
        snapshot=snapshot,
        query_file_sha256=query_file_sha256,
        query_count=len(query_file.queries),
        search_failures=failures,
    )
    return ValidationResult(
        generation_id=generation_id,
        checksum=snapshot.checksum,
        chunk_count=snapshot.chunk_count,
        manifest_revision=snapshot.manifest.revision,
        query_count=len(query_file.queries),
    )


def _validate_t1(
    store: PgVectorStore,
    *,
    space_key: str,
    generation_id: int,
    query_file: ValidationQueryFile,
) -> _T1Snapshot:
    profile = store._require_profile()
    with store._connect() as conn, conn.cursor() as cur:
        store._set_lock_timeout(cur)
        store._lock_space_for_update(cur, space_key)
        target = store._get_generation(
            cur, space_key=space_key, generation_id=generation_id, for_update=True
        )
        if target is None:
            raise GenerationTargetNotFoundError("Validation target was not found in this space")
        if target.state is not GenerationState.BUILDING:
            raise GenerationTargetStateMismatchError(
                f"Validation target must be building, got {target.state.value}"
            )
        store._assert_matching_profile(target, profile)
        cur.execute(
            """
            SELECT g.profile_id, m.state, m.revision, m.source_counts, m.total_chunks
              FROM space_generation g
              LEFT JOIN generation_manifest m
                ON m.space_key = g.space_key AND m.generation_id = g.generation_id
             WHERE g.space_key = %s AND g.generation_id = %s
            """,
            (space_key, generation_id),
        )
        manifest_row = cur.fetchone()
        if manifest_row is None or manifest_row[1] != "complete":
            raise StructuralGenerationValidationError("Manifest must exist in complete state")
        manifest = ManifestSnapshot(
            state=str(manifest_row[1]),
            revision=int(manifest_row[2]),
            source_counts={str(k): int(v) for k, v in dict(manifest_row[3]).items()},
            total_chunks=int(manifest_row[4]),
        )
        cur.execute(
            """
            SELECT chunk_id, source_id, title, heading, breadcrumb, content
              FROM chunks
             WHERE space_key = %s AND generation_id = %s
             ORDER BY chunk_id
            """,
            (space_key, generation_id),
        )
        chunk_rows = cur.fetchall()
        _validate_chunk_rows(chunk_rows, manifest=manifest)
        _validate_vectors(
            cur,
            space_key=space_key,
            generation_id=generation_id,
            dimension=profile.dimension,
        )
        _validate_coverage(query_file, chunk_rows)
        checksum, chunk_count = store.generation_checksum(
            cur, space_key=space_key, generation_id=generation_id
        )
        return _T1Snapshot(
            manifest=manifest,
            checksum=checksum,
            chunk_count=chunk_count,
            profile_id=str(manifest_row[0]),
        )


def _validate_chunk_rows(rows: list[tuple[Any, ...]], *, manifest: ManifestSnapshot) -> None:
    if not rows:
        raise StructuralGenerationValidationError("Generation contains zero chunks")
    counts: Counter[str] = Counter()
    sequences: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        chunk_id, source_id, title, heading, breadcrumb, content = row
        source = "" if source_id is None else str(source_id)
        if not source.strip():
            raise StructuralGenerationValidationError("Chunk source_id must not be blank")
        if not str(title).strip() or not str(content).strip():
            raise StructuralGenerationValidationError("Chunk title and content must not be blank")
        if heading is not None and not str(heading).strip():
            raise StructuralGenerationValidationError("Chunk heading must be null or nonblank")
        if breadcrumb is not None and not str(breadcrumb).strip():
            raise StructuralGenerationValidationError("Chunk breadcrumb must be null or nonblank")
        try:
            parsed_source, raw_sequence = str(chunk_id).rsplit("#", 1)
        except ValueError as exc:
            raise StructuralGenerationValidationError(f"Invalid chunk_id: {chunk_id}") from exc
        if parsed_source != source or not raw_sequence.isdigit():
            raise StructuralGenerationValidationError(f"Invalid chunk_id sequence: {chunk_id}")
        counts[source] += 1
        sequences[source].append(int(raw_sequence))
    for source, values in sequences.items():
        if set(values) != set(range(counts[source])) or len(values) != counts[source]:
            raise StructuralGenerationValidationError(f"Non-contiguous chunk sequence for {source}")
    actual_counts = dict(counts)
    if manifest.source_counts != actual_counts:
        raise StructuralGenerationValidationError(
            "Manifest source_counts do not match stored chunks"
        )
    if manifest.total_chunks != sum(actual_counts.values()) or manifest.total_chunks != len(rows):
        raise StructuralGenerationValidationError(
            "Manifest total_chunks does not match stored chunks"
        )


def _validate_vectors(cur: Any, *, space_key: str, generation_id: int, dimension: int) -> None:
    # pgvector rejects NaN and infinities at storage time (SQLSTATE 22000); the
    # integration test protects that engine boundary, so no unreachable predicate is
    # duplicated here.
    cur.execute(
        """
        SELECT count(*) FILTER (WHERE dims <> %s),
               count(*) FILTER (WHERE l2_norm < 1e-6),
               count(*) FILTER (WHERE spread < 1e-12)
          FROM (
              SELECT c.id, vector_dims(c.embedding) AS dims,
                     sqrt(sum(component * component)) AS l2_norm,
                     max(component) - min(component) AS spread
                FROM chunks c
                CROSS JOIN LATERAL unnest(c.embedding::real[]) AS component
               WHERE c.space_key = %s AND c.generation_id = %s
               GROUP BY c.id, c.embedding
          ) vectors
        """,
        (dimension, space_key, generation_id),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("Vector validation query returned no row")
    if int(row[0]) or int(row[1]) or int(row[2]):
        raise StructuralGenerationValidationError(
            "Generation contains wrong-dimension, zero, or degenerate vectors"
        )


def _validate_coverage(query_file: ValidationQueryFile, rows: list[tuple[Any, ...]]) -> None:
    sources = {str(row[1]) for row in rows}
    covered = {item.expected_source_id for item in query_file.queries}
    missing_sources = sorted(sources - covered)
    if missing_sources:
        raise ValidationQueryConfigurationError(
            f"Queries do not cover source ids: {', '.join(missing_sources)}"
        )
    headings: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if row[3] is not None:
            headings[str(row[1])].add(str(row[3]))
    for source_id, source_headings in headings.items():
        if len(source_headings) < 2:
            continue
        intents = [
            item
            for item in query_file.queries
            if item.category is QueryCategory.HEADING_INTENT
            and item.expected_source_id == source_id
        ]
        expected = {item.expected_heading for item in intents}
        if len(intents) < 2 or len(expected) != len(intents):
            raise ValidationQueryConfigurationError(
                f"Source {source_id} requires two distinct heading_intent queries"
            )


def _query_failure(
    query: ValidationQuery,
    hits: list[ValidationHit],
    *,
    generation_id: int,
) -> str | None:
    if any(hit.generation_id != generation_id for hit in hits):
        return "result contains another generation"
    if not hits:
        return "no search results"
    limit = 3 if query.critical else 5
    for hit in hits[:limit]:
        if hit.source_id != query.expected_source_id:
            continue
        if query.expected_heading is None or hit.heading == query.expected_heading:
            return None
    return f"expected source/heading absent from top-{limit}"


def _validate_t2(
    store: PgVectorStore,
    *,
    space_key: str,
    generation_id: int,
    snapshot: _T1Snapshot,
    query_file_sha256: str,
    query_count: int,
    search_failures: list[str],
) -> None:
    with store._connect() as conn, conn.cursor() as cur:
        store._set_lock_timeout(cur)
        store._lock_space_for_update(cur, space_key)
        target = store._get_generation(
            cur, space_key=space_key, generation_id=generation_id, for_update=True
        )
        if target is None or target.state is not GenerationState.BUILDING:
            raise GenerationConcurrencyError("Validation target changed before T2")
        cur.execute(
            """
            SELECT state, revision, source_counts, total_chunks
              FROM generation_manifest
             WHERE space_key = %s AND generation_id = %s
            """,
            (space_key, generation_id),
        )
        row = cur.fetchone()
        current_manifest = (
            None
            if row is None
            else ManifestSnapshot(
                state=str(row[0]),
                revision=int(row[1]),
                source_counts={str(k): int(v) for k, v in dict(row[2]).items()},
                total_chunks=int(row[3]) if row[3] is not None else -1,
            )
        )
        if current_manifest != snapshot.manifest:
            raise GenerationConcurrencyError("Manifest changed between T1 and T2")
        checksum, chunk_count = store.generation_checksum(
            cur, space_key=space_key, generation_id=generation_id
        )
        if checksum != snapshot.checksum or chunk_count != snapshot.chunk_count:
            raise GenerationConcurrencyError("Chunks changed between T1 and T2")
        if search_failures:
            raise SearchGenerationValidationError("; ".join(search_failures))
        cur.execute(
            """
            INSERT INTO generation_validation
                (space_key, generation_id, checksum, chunk_count, manifest_revision,
                 profile_id, query_file_sha256, query_count, validator_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                space_key,
                generation_id,
                snapshot.checksum,
                snapshot.chunk_count,
                snapshot.manifest.revision,
                snapshot.profile_id,
                query_file_sha256,
                query_count,
                _VALIDATOR_VERSION,
            ),
        )
        cur.execute(
            """
            UPDATE space_generation
               SET state = 'sealed'
             WHERE space_key = %s AND generation_id = %s AND state = 'building'
            """,
            (space_key, generation_id),
        )
        if cur.rowcount != 1:
            raise GenerationConcurrencyError("Validation could not seal one building target")
