"""Vector store — plain pgvector, portable across Postgres / Cloud SQL / AlloyDB."""

from databridge.store.exceptions import EmbeddingProfileMismatchError
from databridge.store.pg import PgVectorStore, SearchHit
from databridge.store.provenance import Generation, GenerationState, ProfileMode

__all__ = [
    "EmbeddingProfileMismatchError",
    "Generation",
    "GenerationState",
    "PgVectorStore",
    "ProfileMode",
    "SearchHit",
]
