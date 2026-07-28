"""HTTP provenance preflight and existing refusal-contract tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import NoReturn

import pytest
from fastapi import HTTPException

import databridge.server.app as server_app
from databridge.agents.runtime import NoEvidenceError
from databridge.store import EmbeddingProfileMismatchError


class _PreflightStore:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def preflight(self, *, space_key: str) -> None:
        self.calls.append(space_key)
        if self.error is not None:
            raise self.error


@pytest.mark.asyncio
async def test_startup_preflight_propagates_profile_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _PreflightStore(EmbeddingProfileMismatchError("mismatch"))
    monkeypatch.setattr(
        server_app,
        "get_deps",
        lambda: SimpleNamespace(store=store, space_key="MFS"),
    )

    with pytest.raises(EmbeddingProfileMismatchError):
        async with server_app.lifespan(server_app.app):
            pass
    assert store.calls == ["MFS"]


@pytest.mark.asyncio
async def test_ask_returns_sanitized_503_before_agent_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _PreflightStore(EmbeddingProfileMismatchError("secret fingerprint"))
    monkeypatch.setattr(
        server_app,
        "get_deps",
        lambda: SimpleNamespace(store=store, space_key="MFS"),
    )

    async def must_not_run(question: str) -> NoReturn:
        raise AssertionError(f"agent unexpectedly ran for {question}")

    monkeypatch.setattr(server_app, "ask_async", must_not_run)
    with pytest.raises(HTTPException) as captured:
        await server_app.ask_endpoint(server_app.AskRequest(question="question"))

    assert captured.value.status_code == 503
    assert captured.value.detail == "Knowledge index configuration is incompatible"
    assert "fingerprint" not in str(captured.value.detail)
    assert store.calls == ["MFS"]


@pytest.mark.asyncio
async def test_ask_no_evidence_422_body_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _PreflightStore()
    monkeypatch.setattr(
        server_app,
        "get_deps",
        lambda: SimpleNamespace(store=store, space_key="MFS"),
    )
    message = (
        "I could not find supporting evidence in the knowledge base, "
        "so I cannot give a grounded answer."
    )

    async def refuse(question: str) -> NoReturn:
        del question
        raise NoEvidenceError(message)

    monkeypatch.setattr(server_app, "ask_async", refuse)
    with pytest.raises(HTTPException) as captured:
        await server_app.ask_endpoint(server_app.AskRequest(question="question"))

    assert captured.value.status_code == 422
    assert captured.value.detail == message
    assert store.calls == ["MFS"]
