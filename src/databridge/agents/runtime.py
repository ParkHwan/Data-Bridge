"""Run the AI team and return a grounded result with a collaboration trace.

Citation policy (strict — post-review):
- Document evidence: the model must emit "SOURCES: [n]" markers naming the
  search_knowledge refs it used. Markers referencing real evidence become document
  citations. **No valid markers → NoEvidenceError** — silently citing everything that
  was retrieved would dress an ungrounded answer in green checkmarks.
- BigQuery evidence: every successful query_bigquery call is deterministic evidence;
  the exact SQL + referenced tables become a bigquery citation automatically.

The collaboration trace (which agent acted, which tool ran) is a first-class output so
the UI can show the AI team working — not just the final text (post-review metadata
contract).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from google.adk.runners import InMemoryRunner
from google.genai import types

from databridge.agents.team import build_root_agent
from databridge.citations import Citation, GroundedAnswer

_APP_NAME = "databridge"
_SOURCES_RE = re.compile(r"SOURCES:\s*((?:\[\d+\][,\s]*)*)", re.IGNORECASE)
_REF_RE = re.compile(r"\[(\d+)\]")

_REFUSAL = (
    "I could not find supporting evidence in the knowledge base, "
    "so I cannot give a grounded answer."
)


@dataclass(frozen=True, slots=True)
class TraceStep:
    """One observable step of the AI team — for the UI collaboration view."""

    agent: str
    kind: str  # "tool_call" | "tool_result" | "final"
    detail: str


@dataclass(frozen=True, slots=True)
class TeamResult:
    grounded: GroundedAnswer
    trace: tuple[TraceStep, ...]

    @property
    def answer(self) -> str:
        return self.grounded.answer

    @property
    def citations(self) -> tuple[Citation, ...]:
        return self.grounded.citations


class NoEvidenceError(RuntimeError):
    """Raised when the team produced no citable evidence for the question."""


def ask(question: str, *, user_id: str = "local") -> TeamResult:
    return asyncio.run(ask_async(question, user_id=user_id))


async def ask_async(question: str, *, user_id: str = "local") -> TeamResult:
    runner = InMemoryRunner(agent=build_root_agent(), app_name=_APP_NAME)
    session = await runner.session_service.create_session(app_name=_APP_NAME, user_id=user_id)
    message = types.Content(role="user", parts=[types.Part(text=question)])

    doc_evidence: dict[int, dict[str, Any]] = {}
    bq_evidence: list[dict[str, Any]] = []
    trace: list[TraceStep] = []
    final_text = ""

    async for event in runner.run_async(
        user_id=user_id, session_id=session.id, new_message=message
    ):
        agent = getattr(event, "author", "") or "unknown"
        for part in _parts(event):
            call = getattr(part, "function_call", None)
            if call is not None:
                trace.append(TraceStep(agent=agent, kind="tool_call", detail=call.name))
            response = getattr(part, "function_response", None)
            if response is not None:
                trace.append(TraceStep(agent=agent, kind="tool_result", detail=response.name))
                _collect_evidence(response, doc_evidence, bq_evidence)
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(p.text or "" for p in event.content.parts)
            trace.append(TraceStep(agent=agent, kind="final", detail=final_text[:80]))

    grounded = _to_grounded_answer(final_text, doc_evidence, bq_evidence)
    return TeamResult(grounded=grounded, trace=tuple(trace))


def _parts(event: Any) -> list[Any]:
    content = getattr(event, "content", None)
    return list(getattr(content, "parts", None) or [])


def _collect_evidence(
    response: Any,
    doc_evidence: dict[int, dict[str, Any]],
    bq_evidence: list[dict[str, Any]],
) -> None:
    payload = response.response or {}
    body = payload.get("result", payload) if isinstance(payload, dict) else payload
    if response.name == "search_knowledge" and isinstance(body, list):
        for item in body:
            if isinstance(item, dict) and isinstance(item.get("ref"), int):
                doc_evidence[item["ref"]] = item
    elif response.name == "query_bigquery" and isinstance(body, dict) and body.get("sql"):
        bq_evidence.append(body)


def _to_grounded_answer(
    text: str,
    doc_evidence: dict[int, dict[str, Any]],
    bq_evidence: list[dict[str, Any]],
) -> GroundedAnswer:
    citations: list[Citation] = []

    # BigQuery evidence is deterministic: the SQL that ran is the citation.
    for item in bq_evidence:
        tables = item.get("referenced_tables") or ["unknown.unknown.unknown"]
        citations.append(
            Citation(
                kind="bigquery",
                source_id=str(tables[0]),
                sql=str(item["sql"]),
                snippet=f"{item.get('row_count_returned', '?')} rows returned",
            )
        )

    # Document evidence requires explicit SOURCES markers (strict — no fallback:
    # citing all retrieved chunks would fabricate confidence, post-review P1).
    cited_refs = [r for r in _parse_cited_refs(text) if r in doc_evidence]
    for ref in cited_refs:
        item = doc_evidence[ref]
        citations.append(
            Citation(
                kind="document",
                source_id=str(item["source_id"]),
                title=item.get("title"),
                heading=item.get("heading") or None,
                breadcrumb=item.get("breadcrumb"),
                snippet=(str(item.get("content", ""))[:200] or None),
            )
        )

    if not citations:
        raise NoEvidenceError(_REFUSAL)

    answer = _SOURCES_RE.sub("", text).strip()
    if not answer:
        raise NoEvidenceError(_REFUSAL)
    return GroundedAnswer(answer=answer, citations=tuple(citations))


def _parse_cited_refs(text: str) -> list[int]:
    refs: list[int] = []
    for match in _SOURCES_RE.finditer(text):
        refs.extend(int(m) for m in _REF_RE.findall(match.group(1)))
    seen: set[int] = set()
    unique = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique
