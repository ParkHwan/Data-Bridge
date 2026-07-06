"""FastAPI app — POST /ask runs the AI team; GET / serves the demo UI.

The response mirrors the internal contracts 1:1 (GroundedAnswer + trace): the UI is a
renderer, never a source of truth (design D-12, §4.1).
"""

from __future__ import annotations

from importlib import resources
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from databridge.agents.runtime import NoEvidenceError, ask_async

app = FastAPI(title="Data Bridge", version="0.2.0")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class CitationOut(BaseModel):
    kind: str
    source_id: str
    title: str | None = None
    heading: str | None = None
    breadcrumb: str | None = None
    sql: str | None = None
    snippet: str | None = None


class TraceStepOut(BaseModel):
    agent: str
    kind: str
    detail: str


class AskResponse(BaseModel):
    answer: str
    citations: list[CitationOut]
    trace: list[TraceStepOut]


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True}


@app.post("/ask", response_model=AskResponse)
async def ask_endpoint(request: AskRequest) -> AskResponse:
    try:
        result = await ask_async(request.question)
    except NoEvidenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AskResponse(
        answer=result.answer,
        citations=[CitationOut(**c.model_dump()) for c in result.citations],
        trace=[
            TraceStepOut(agent=s.agent, kind=s.kind, detail=s.detail) for s in result.trace
        ],
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return resources.files("databridge.server").joinpath("index.html").read_text("utf-8")
