"""Agent tools — plain functions with JSON-friendly signatures (ADK auto-wraps)."""

from __future__ import annotations

from typing import Any

from databridge.agents.deps import get_deps


def search_knowledge(query: str) -> list[dict[str, Any]]:
    """Search the ingested knowledge base for evidence relevant to the query.

    Returns the most relevant document chunks. Every returned item is citable:
    ``source_id`` + ``heading`` locate the evidence inside a real document. Always call
    this before answering a knowledge question; never answer from memory.
    """
    deps = get_deps()
    query_embedding = deps.embedder.embed([query])[0]
    hits = deps.store.search_hybrid(
        query_embedding, query, space_key=deps.space_key, top_k=5
    )
    return [
        {
            "ref": i + 1,
            "source_id": h.source_id,
            "title": h.title,
            "heading": h.heading,
            "breadcrumb": h.breadcrumb,
            "content": h.content,
        }
        for i, h in enumerate(hits)
    ]
