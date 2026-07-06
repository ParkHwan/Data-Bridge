"""Vertex AI embedder — gemini-embedding-001 via the google-genai SDK.

Requires ``pip install databridge[gcp]`` and Application Default Credentials with a
billing-enabled project. Env: GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION.
"""

from __future__ import annotations

import os
import time
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
        result = self._embed_with_retry(texts)
        vectors = [list(e.values) for e in result.embeddings]
        for vec in vectors:
            if len(vec) != EMBEDDING_DIM:
                msg = f"vertex returned dimension {len(vec)} != {EMBEDDING_DIM}"
                raise RuntimeError(msg)
        return vectors

    def _embed_with_retry(self, texts: list[str], *, attempts: int = 3) -> Any:
        """Retry transient API failures (429/5xx) with exponential backoff.

        Batch ingest should survive a rate-limit blip; retries never persist partial
        data (unlike a fallback would), so this stays consistent with the
        grounded-or-nothing posture.
        """
        delay = 2.0
        for attempt in range(1, attempts + 1):
            try:
                return self._client.models.embed_content(
                    model=_MODEL,
                    contents=texts,
                    config={"output_dimensionality": EMBEDDING_DIM},
                )
            except Exception as exc:  # google-genai raises SDK-specific errors
                status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                transient = status in (429, 500, 502, 503, 504)
                if not transient or attempt == attempts:
                    raise
                time.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")
