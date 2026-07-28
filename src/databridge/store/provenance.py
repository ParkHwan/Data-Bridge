"""Database-facing embedding provenance value objects."""

from __future__ import annotations

import os
from collections.abc import Mapping
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


class ProfileModeConfigurationError(RuntimeError):
    """Raised when embedding-provenance enforcement mode is not explicit."""


def resolve_profile_mode(environ: Mapping[str, str] | None = None) -> ProfileMode:
    """Resolve the required observe/strict deployment mode without a default."""
    values = os.environ if environ is None else environ
    raw = values.get("DATABRIDGE_PROFILE_MODE")
    if raw is None or not raw.strip():
        raise ProfileModeConfigurationError(
            "DATABRIDGE_PROFILE_MODE is required; allowed values: observe, strict"
        )
    try:
        return ProfileMode(raw.strip().lower())
    except ValueError as exc:
        raise ProfileModeConfigurationError(
            f"Unsupported DATABRIDGE_PROFILE_MODE={raw!r}; "
            "allowed values: observe, strict"
        ) from exc


@dataclass(frozen=True, slots=True)
class Generation:
    generation_id: int
    space_key: str
    profile: EmbeddingProfile
    state: GenerationState


def profile_id_for(profile: EmbeddingProfile) -> str:
    """Use the canonical configuration fingerprint as the deterministic row id."""
    return profile.config_fingerprint
