"""Vertex AI embedder — gemini-embedding-001 via the google-genai SDK.

Requires ``pip install databridge[gcp]`` and Application Default Credentials with a
billing-enabled project. Env: GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION.
"""

from __future__ import annotations

import os
from typing import Any

from databridge.embed.base import EMBEDDING_DIM

_MODEL = "gemini-embedding-001"


class VertexEmbedder:
    def __init__(self, *, project: str | None = None, location: str | None = None) -> None:
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - env-dependent
            msg = "google-genai is required: pip install 'databridge[gcp]'"
            raise RuntimeError(msg) from exc
        self._client: Any = genai.Client(
            vertexai=True,
            project=project or os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=location or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = self._client.models.embed_content(
            model=_MODEL,
            contents=texts,
            config={"output_dimensionality": EMBEDDING_DIM},
        )
        return [list(e.values) for e in result.embeddings]
