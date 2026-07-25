"""Fail-safe orchestration for a complete Confluence folder ingestion run."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from databridge.confluence.adapter import page_to_source_document
from databridge.confluence.ancestors import AncestorResolver
from databridge.confluence.exceptions import BatchAlreadyRunningError, BatchSafetyError
from databridge.confluence.models import Page, Space
from databridge.confluence.parser import ADFParser
from databridge.embed.base import Embedder
from databridge.ingest.chunker import Chunk, chunk_document

logger = logging.getLogger(__name__)


class BatchClient(Protocol):
    async def get_space_by_key(self, space_key: str) -> Space: ...

    async def get_folder(self, folder_id: str) -> dict[str, object]: ...

    def iter_folder_descendants(
        self, folder_id: str, *, batch_size: int = 50, depth: int = 10
    ) -> AsyncIterator[Page]: ...

    async def get_page(self, page_id: str, *, body_format: str = "atlas_doc_format") -> Page: ...


class BatchStore(Protocol):
    def advisory_lock(self, key: str) -> AbstractContextManager[bool]: ...

    def replace_source(
        self,
        *,
        space_key: str,
        source_id: str,
        chunks: list[Chunk],
        embeddings: list[list[float]],
    ) -> int: ...

    def list_source_ids(self, *, space_key: str) -> set[str]: ...

    def delete_source(self, *, space_key: str, source_id: str) -> int: ...


@dataclass(frozen=True, slots=True)
class ConfluenceBatchConfig:
    space_key: str
    folder_id: str
    max_pages: int = 500
    max_chunks: int = 5_000
    batch_size: int = 50
    descendant_depth: int = 10
    embed_batch_size: int = 100

    def __post_init__(self) -> None:
        if not self.space_key.strip() or not self.folder_id.strip():
            raise ValueError("space_key and folder_id must not be empty")
        for name in (
            "max_pages",
            "max_chunks",
            "batch_size",
            "descendant_depth",
            "embed_batch_size",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")

    @property
    def lock_key(self) -> str:
        return f"databridge:confluence:{self.space_key}:{self.folder_id}"


@dataclass(frozen=True, slots=True)
class BatchResult:
    pages: int
    chunks: int
    vertex_calls: int
    deleted_sources: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class _PreparedPage:
    page_id: str
    chunks: list[Chunk]
    embeddings: list[list[float]]


async def run_confluence_batch(
    *,
    client: BatchClient,
    store: BatchStore,
    embedder: Embedder,
    config: ConfluenceBatchConfig,
    parser: ADFParser | None = None,
) -> BatchResult:
    """Run a full snapshot ingest and garbage-collect only after full success.

    Traversal, page fetches, parsing, chunking, and all embedding calls finish before
    the first store mutation. A failure in any of those phases therefore cannot remove
    or partially refresh the previous complete snapshot.
    """
    started = time.monotonic()
    with store.advisory_lock(config.lock_key) as acquired:
        if not acquired:
            raise BatchAlreadyRunningError("Another Confluence ingest run is active")
        return await _run_locked(
            client=client,
            store=store,
            embedder=embedder,
            config=config,
            parser=parser or ADFParser(),
            started=started,
        )


async def _run_locked(
    *,
    client: BatchClient,
    store: BatchStore,
    embedder: Embedder,
    config: ConfluenceBatchConfig,
    parser: ADFParser,
    started: float,
) -> BatchResult:
    space = await client.get_space_by_key(config.space_key)
    folder = await client.get_folder(config.folder_id)
    folder_space_id = folder.get("spaceId") or folder.get("space_id")
    if folder_space_id is not None and str(folder_space_id) != space.id:
        raise BatchSafetyError("Configured folder does not belong to the configured space")

    descendant_ids = await _collect_page_ids(client, config)
    resolver = AncestorResolver(client)
    prepared: list[_PreparedPage] = []
    total_chunks = 0
    vertex_calls = 0

    for page_id in descendant_ids:
        page = await client.get_page(page_id, body_format="atlas_doc_format")
        if page.space_id != space.id:
            raise BatchSafetyError(
                f"Page {page.id} belongs to space {page.space_id!r}, expected {space.id!r}"
            )
        resolver.seed_page(page)
        ancestors = await resolver.resolve(page.parent_id, page.parent_type)
        breadcrumb = " > ".join(ancestors) or None
        document = page_to_source_document(
            page,
            space_key=config.space_key,
            breadcrumb=breadcrumb,
            parser=parser,
        )
        chunks = chunk_document(document)
        if not chunks:
            raise BatchSafetyError(f"Page {page.id} produced no chunks")
        total_chunks += len(chunks)
        if total_chunks > config.max_chunks:
            raise BatchSafetyError(
                f"Chunk count exceeds safety cap: {total_chunks} > {config.max_chunks}"
            )
        embeddings: list[list[float]] = []
        for offset in range(0, len(chunks), config.embed_batch_size):
            chunk_batch = chunks[offset : offset + config.embed_batch_size]
            embeddings.extend(embedder.embed([chunk.embedding_text for chunk in chunk_batch]))
            vertex_calls += 1
        if len(embeddings) != len(chunks):
            raise BatchSafetyError(
                f"Embedder returned {len(embeddings)} vectors for {len(chunks)} chunks"
            )
        prepared.append(_PreparedPage(page.id, chunks, embeddings))

    for item in prepared:
        store.replace_source(
            space_key=config.space_key,
            source_id=item.page_id,
            chunks=item.chunks,
            embeddings=item.embeddings,
        )

    seen = {item.page_id for item in prepared}
    stale = store.list_source_ids(space_key=config.space_key) - seen
    for source_id in sorted(stale):
        store.delete_source(space_key=config.space_key, source_id=source_id)
    elapsed = time.monotonic() - started
    logger.info(
        "Confluence ingest complete: space=%s pages=%s chunks=%s vertex_calls=%s "
        "deleted_sources=%s elapsed_seconds=%.3f",
        config.space_key,
        len(prepared),
        total_chunks,
        vertex_calls,
        len(stale),
        elapsed,
    )
    return BatchResult(len(prepared), total_chunks, vertex_calls, len(stale), elapsed)


async def _collect_page_ids(client: BatchClient, config: ConfluenceBatchConfig) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    async for descendant in client.iter_folder_descendants(
        config.folder_id,
        batch_size=config.batch_size,
        depth=config.descendant_depth,
    ):
        if getattr(descendant, "type", None) != "page" or descendant.id in seen:
            continue
        seen.add(descendant.id)
        ids.append(descendant.id)
        if len(ids) > config.max_pages:
            raise BatchSafetyError(
                f"Page count exceeds safety cap: {len(ids)} > {config.max_pages}"
            )
    if not ids:
        raise BatchSafetyError("Traversal returned no pages; refusing destructive GC")
    return ids
