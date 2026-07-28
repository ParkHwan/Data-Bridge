"""Vector store — plain pgvector, portable across Postgres / Cloud SQL / AlloyDB."""

from databridge.store.exceptions import EmbeddingProfileMismatchError
from databridge.store.pg import PgVectorStore, SearchHit
from databridge.store.provenance import (
    Generation,
    GenerationChunkCount,
    GenerationState,
    ProfileMode,
    ProfileModeConfigurationError,
    SpaceProfileReport,
    resolve_profile_mode,
)

__all__ = [
    "EmbeddingProfileMismatchError",
    "Generation",
    "GenerationChunkCount",
    "GenerationState",
    "PgVectorStore",
    "ProfileMode",
    "ProfileModeConfigurationError",
    "SearchHit",
    "SpaceProfileReport",
    "resolve_profile_mode",
]
