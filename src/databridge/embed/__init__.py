"""Embedding providers behind one protocol (portable profile, design D-3/D-4)."""

from databridge.embed.base import EMBEDDING_DIM, Embedder
from databridge.embed.hashed import HashedEmbedder

__all__ = ["EMBEDDING_DIM", "Embedder", "HashedEmbedder"]
