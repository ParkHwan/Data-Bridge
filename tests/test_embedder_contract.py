"""Embedding selection and identity contracts (no provider calls)."""

from __future__ import annotations

import importlib.util
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import ModuleType

import pytest

from databridge.agents import deps
from databridge.embed import (
    EMBEDDING_DIM,
    EmbedderConfigurationError,
    HashedEmbedder,
    resolve_embedder,
)
from databridge.embed.base import make_config_fingerprint
from databridge.embed.vertex import _MODEL, VertexEmbedder

_ROOT = Path(__file__).parents[1]


def _load_script(name: str) -> ModuleType:
    path = _ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Could not load script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolver_rejects_missing_configuration() -> None:
    with pytest.raises(EmbedderConfigurationError, match="DATABRIDGE_EMBEDDER is required") as exc:
        resolve_embedder({})
    assert "hashed, vertex" in str(exc.value)
    assert "Ingestion and query paths must use the same value" in str(exc.value)


def test_resolver_rejects_unsupported_value() -> None:
    with pytest.raises(EmbedderConfigurationError, match="Unsupported") as exc:
        resolve_embedder({"DATABRIDGE_EMBEDDER": "vertx"})
    assert "hashed, vertex" in str(exc.value)
    assert "Ingestion and query paths must use the same value" in str(exc.value)


def test_resolver_rejects_legacy_alias_with_migration_hint() -> None:
    with pytest.raises(EmbedderConfigurationError, match="no longer supported") as exc:
        resolve_embedder({"EMBEDDER": "vertex"})
    assert "use DATABRIDGE_EMBEDDER" in str(exc.value)


def test_resolver_builds_hashed() -> None:
    assert isinstance(resolve_embedder({"DATABRIDGE_EMBEDDER": " hashed "}), HashedEmbedder)


def test_resolver_builds_vertex(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr("databridge.embed.vertex.VertexEmbedder", lambda: sentinel)
    assert resolve_embedder({"DATABRIDGE_EMBEDDER": "VERTEX"}) is sentinel


def test_embedding_profiles_are_immutable_and_identify_vector_spaces() -> None:
    hashed = HashedEmbedder().profile
    vertex = object.__new__(VertexEmbedder).profile

    with pytest.raises(FrozenInstanceError):
        hashed.dimension = 1
    assert hashed.provider == "hashed"
    assert hashed.dimension == EMBEDDING_DIM
    assert vertex.provider == "vertex"
    assert vertex.model == _MODEL == "gemini-embedding-001"
    assert vertex.dimension == EMBEDDING_DIM
    assert hashed.config_fingerprint != vertex.config_fingerprint


def test_config_fingerprint_includes_dimension() -> None:
    values = {"provider": "p", "model": "m", "options": {"mode": "document"}}
    full = make_config_fingerprint(dimension=768, **values)
    reduced = make_config_fingerprint(dimension=384, **values)
    assert full != reduced


def test_sample_ingest_uses_shared_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    ingest_samples = _load_script("ingest_samples")
    sentinel = object()
    monkeypatch.setattr(ingest_samples, "resolve_embedder", lambda: sentinel)
    assert ingest_samples.make_embedder() is sentinel


def test_confluence_ingest_uses_shared_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    ingest_confluence = _load_script("ingest_confluence")
    sentinel = object()
    monkeypatch.setattr(ingest_confluence, "resolve_embedder", lambda: sentinel)
    assert ingest_confluence._make_embedder() is sentinel


def test_agent_runtime_uses_shared_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr(deps, "resolve_embedder", lambda: sentinel)
    built = deps._build_default()
    assert built.embedder is sentinel


def test_iac_pins_embedder_for_all_runtime_resources() -> None:
    script = (_ROOT / "scripts" / "setup_cicd.sh").read_text()
    assert "--update-env-vars \"DATABRIDGE_EMBEDDER=vertex\"" in script
    assert (
        "--update-env-vars \"DATABRIDGE_SPACE=${DATABRIDGE_SPACE},"
        "DATABRIDGE_EMBEDDER=vertex\""
    ) in script
    assert '"$SERVICE_EMBEDDER" == "vertex"' in script
    assert script.count("DATABRIDGE_EMBEDDER=vertex") >= 4
