"""Database-facing embedding provenance value objects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from databridge.embed import EmbeddingProfile


class GenerationState(StrEnum):
    BUILDING = "building"
    ACTIVE = "active"
    RETIRED = "retired"


class ProfileMode(StrEnum):
    OBSERVE = "observe"
    STRICT = "strict"


@dataclass(frozen=True, slots=True)
class Generation:
    generation_id: int
    space_key: str
    profile: EmbeddingProfile
    state: GenerationState


def profile_id_for(profile: EmbeddingProfile) -> str:
    """Use the canonical configuration fingerprint as the deterministic row id."""
    return profile.config_fingerprint
