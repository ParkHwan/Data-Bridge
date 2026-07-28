"""Strict, shared selection of the embedding vector space.

This prevents new ingest/query configuration mismatches. Profiles are not persisted
until the follow-up provenance migration, so this module cannot detect whether an
already stored index was produced by the selected embedder.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from databridge.embed.base import Embedder

_ALLOWED = ("hashed", "vertex")
_REASON = (
    "Ingestion and query paths must use the same value so their vector spaces match."
)


class EmbedderConfigurationError(RuntimeError):
    """Raised when the process has no unambiguous embedding configuration."""


def resolve_embedder(environ: Mapping[str, str] | None = None) -> Embedder:
    """Build the explicitly configured embedder without defaults or fallbacks."""
    values = os.environ if environ is None else environ
    raw = values.get("DATABRIDGE_EMBEDDER")
    if raw is None:
        if "EMBEDDER" in values:
            raise EmbedderConfigurationError(
                "EMBEDDER is no longer supported; use DATABRIDGE_EMBEDDER. " + _requirement()
            )
        raise EmbedderConfigurationError(_requirement())

    kind = raw.strip().lower()
    if not kind:
        raise EmbedderConfigurationError(_requirement())
    if kind == "hashed":
        from databridge.embed.hashed import HashedEmbedder

        return HashedEmbedder()
    if kind == "vertex":
        from databridge.embed.vertex import VertexEmbedder

        return VertexEmbedder()
    allowed = ", ".join(_ALLOWED)
    raise EmbedderConfigurationError(
        f"Unsupported DATABRIDGE_EMBEDDER={raw!r}; allowed values: {allowed}. {_REASON}"
    )


def _requirement() -> str:
    allowed = ", ".join(_ALLOWED)
    return f"DATABRIDGE_EMBEDDER is required; allowed values: {allowed}. {_REASON}"
