"""Embedder protocol.

The app owns embedding generation (design D-3 portability profile): the vector store
never calls model APIs itself, so Postgres/pgvector, Cloud SQL, and AlloyDB stay
interchangeable. Dimension is pinned project-wide.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

# gemini-embedding-001 with output_dimensionality=768 (MRL truncation).
# The local dev embedder produces the same dimension so schemas never diverge.
EMBEDDING_DIM = 768


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    """Immutable identity for one vector-compatible embedding configuration."""

    provider: str
    model: str
    dimension: int
    config_fingerprint: str


def make_config_fingerprint(
    *, provider: str, model: str, dimension: int, options: dict[str, str | int | bool]
) -> str:
    """Return a stable fingerprint including every vector-compatibility setting."""
    payload = {
        "dimension": dimension,
        "model": model,
        "options": options,
        "provider": provider,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class Embedder(Protocol):
    @property
    def profile(self) -> EmbeddingProfile:
        """Identify the vector space produced by this embedder."""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one EMBEDDING_DIM-length vector per input text."""
        ...
