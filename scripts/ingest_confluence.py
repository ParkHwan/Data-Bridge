"""Run the configured full Confluence folder ingestion batch."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from databridge.confluence import ConfluenceBatchConfig, ConfluenceClient, run_confluence_batch
from databridge.embed import Embedder, HashedEmbedder
from databridge.store import PgVectorStore

DEFAULT_DSN = "postgresql://databridge:databridge@localhost:5433/databridge"


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def _positive_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be positive")
    return value


def _make_embedder() -> Embedder:
    kind = os.environ.get("DATABRIDGE_EMBEDDER", os.environ.get("EMBEDDER", "vertex")).lower()
    if kind == "vertex":
        from databridge.embed.vertex import VertexEmbedder

        return VertexEmbedder()
    if kind == "hashed":
        return HashedEmbedder()
    raise RuntimeError(f"Unsupported embedder: {kind}")


async def _run() -> None:
    config = ConfluenceBatchConfig(
        space_key=_required_env("SPACE_KEY"),
        folder_id=_required_env("FOLDER_ID"),
        max_pages=_positive_env("CONFLUENCE_MAX_PAGES", 500),
        max_chunks=_positive_env("CONFLUENCE_MAX_CHUNKS", 5_000),
        embed_batch_size=_positive_env("CONFLUENCE_EMBED_BATCH_SIZE", 100),
    )
    store = PgVectorStore(os.environ.get("DATABRIDGE_DSN", DEFAULT_DSN))
    store.ensure_schema()
    async with ConfluenceClient(
        base_url=_required_env("CONFLUENCE_BASE_URL"),
        email=os.environ.get("CONFLUENCE_EMAIL", "").strip(),
        api_token=_required_env("CONFLUENCE_API_TOKEN"),
    ) as client:
        result = await run_confluence_batch(
            client=client,
            store=store,
            embedder=_make_embedder(),
            config=config,
        )
    logging.getLogger(__name__).info("Batch result: %s", result)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
