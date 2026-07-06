"""Ingest samples/docs into the local pgvector store.

Usage:
    docker compose up -d
    uv run python scripts/ingest_samples.py                # HashedEmbedder (no GCP)
    DATABRIDGE_EMBEDDER=vertex uv run python scripts/ingest_samples.py

DSN override: DATABRIDGE_DSN (default: local docker compose instance).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from databridge.embed import Embedder, HashedEmbedder
from databridge.ingest.chunker import chunk_document
from databridge.ingest.markdown import load_markdown_file
from databridge.store import PgVectorStore

DEFAULT_DSN = "postgresql://databridge:databridge@localhost:5433/databridge"


def make_embedder() -> Embedder:
    if os.environ.get("DATABRIDGE_EMBEDDER", "hashed").lower() == "vertex":
        from databridge.embed.vertex import VertexEmbedder

        return VertexEmbedder()
    return HashedEmbedder()


def main() -> int:
    docs_dir = Path(__file__).parents[1] / "samples" / "docs"
    store = PgVectorStore(os.environ.get("DATABRIDGE_DSN", DEFAULT_DSN))
    store.ensure_schema()
    embedder = make_embedder()

    total_chunks = 0
    for path in sorted(docs_dir.glob("*.md")):
        doc = load_markdown_file(path)
        chunks = chunk_document(doc)
        embeddings = embedder.embed([c.embedding_text for c in chunks])
        total_chunks += store.replace_source(
            space_key=doc.space_key,
            source_id=doc.source_id,
            chunks=chunks,
            embeddings=embeddings,
        )
        print(f"ingested {doc.source_id}: {len(chunks)} chunks")
    print(f"done: {total_chunks} chunks total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
