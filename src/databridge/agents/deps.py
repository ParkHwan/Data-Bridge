"""Shared runtime dependencies for agent tools.

ADK tools are plain functions; they resolve the store/embedder through this module's
lazily-initialized singleton so the tool signature stays JSON-schema friendly. The
contracts themselves (citations, GroundedAnswer) stay framework-agnostic (design §4.1).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from databridge.embed import Embedder, resolve_embedder
from databridge.store import PgVectorStore

DEFAULT_DSN = "postgresql://databridge:databridge@localhost:5433/databridge"
DEFAULT_SPACE = "DEMO"


@dataclass(frozen=True, slots=True)
class AgentDeps:
    store: PgVectorStore
    embedder: Embedder
    space_key: str


_DEPS: AgentDeps | None = None


def get_deps() -> AgentDeps:
    global _DEPS
    if _DEPS is None:
        _DEPS = _build_default()
    return _DEPS


def set_deps(deps: AgentDeps) -> None:
    """Test/override hook."""
    global _DEPS
    _DEPS = deps


def _build_default() -> AgentDeps:
    return AgentDeps(
        store=PgVectorStore(os.environ.get("DATABRIDGE_DSN", DEFAULT_DSN)),
        embedder=resolve_embedder(),
        space_key=os.environ.get("DATABRIDGE_SPACE", DEFAULT_SPACE),
    )
