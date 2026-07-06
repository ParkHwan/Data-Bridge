"""Embedder protocol.

The app owns embedding generation (design D-3 portability profile): the vector store
never calls model APIs itself, so Postgres/pgvector, Cloud SQL, and AlloyDB stay
interchangeable. Dimension is pinned project-wide.
"""

from __future__ import annotations

from typing import Protocol

# gemini-embedding-001 with output_dimensionality=768 (MRL truncation).
# The local dev embedder produces the same dimension so schemas never diverge.
EMBEDDING_DIM = 768


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one EMBEDDING_DIM-length vector per input text."""
        ...
