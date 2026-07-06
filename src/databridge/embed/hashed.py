"""Deterministic local-dev embedder (no GCP dependency).

Token-hash bag-of-words vectors: cosine similarity is meaningful enough for tests and
offline development (shared tokens → higher similarity). Never used in deployment —
the Vertex AI embedder (``databridge.embed.vertex``) is the production path.
"""

from __future__ import annotations

import hashlib
import math
import re

from databridge.embed.base import EMBEDDING_DIM

_TOKEN_RE = re.compile(r"[\w가-힣]+")


class HashedEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    @staticmethod
    def _embed_one(text: str) -> list[float]:
        vec = [0.0] * EMBEDDING_DIM
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec
