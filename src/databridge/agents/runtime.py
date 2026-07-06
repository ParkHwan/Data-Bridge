"""Run the AI team and return a GroundedAnswer.

Citations are built from the tool evidence the model actually cited ("SOURCES: [n]"
markers mapped back to search_knowledge results captured from the event stream). If the
model cites nothing usable, we fall back to all retrieved chunks — the retrieved
evidence is what the answer was grounded on. If there is no evidence at all, we refuse
rather than return an uncited answer (grounded or nothing).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from google.adk.runners import InMemoryRunner
from google.genai import types

from databridge.agents.team import build_root_agent
from databridge.citations import Citation, GroundedAnswer

_APP_NAME = "databridge"
_SOURCES_RE = re.compile(r"SOURCES:\s*((?:\[\d+\]\s*)+)", re.IGNORECASE)
_REF_RE = re.compile(r"\[(\d+)\]")

_REFUSAL = (
    "I could not find supporting evidence in the knowledge base, "
    "so I cannot give a grounded answer."
)


def ask(question: str, *, user_id: str = "local") -> GroundedAnswer:
    return asyncio.run(ask_async(question, user_id=user_id))


async def ask_async(question: str, *, user_id: str = "local") -> GroundedAnswer:
    runner = InMemoryRunner(agent=build_root_agent(), app_name=_APP_NAME)
    session = await runner.session_service.create_session(app_name=_APP_NAME, user_id=user_id)
    message = types.Content(role="user", parts=[types.Part(text=question)])

    evidence: dict[int, dict[str, Any]] = {}
    final_text = ""
    async for event in runner.run_async(
        user_id=user_id, session_id=session.id, new_message=message
    ):
        for item in _extract_tool_results(event):
            ref = item.get("ref")
            if isinstance(ref, int):
                evidence[ref] = item
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(p.text or "" for p in event.content.parts)

    return _to_grounded_answer(final_text, evidence)


def _extract_tool_results(event: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    content = getattr(event, "content", None)
    for part in getattr(content, "parts", None) or []:
        response = getattr(part, "function_response", None)
        if response is None or response.name != "search_knowledge":
            continue
        payload = response.response or {}
        rows = payload.get("result", payload) if isinstance(payload, dict) else payload
        if isinstance(rows, list):
            results.extend(r for r in rows if isinstance(r, dict))
    return results


def _to_grounded_answer(text: str, evidence: dict[int, dict[str, Any]]) -> GroundedAnswer:
    if not evidence:
        # No retrieved evidence — refuse instead of emitting an uncited claim.
        raise NoEvidenceError(_REFUSAL)

    cited_refs = _parse_cited_refs(text)
    used = [evidence[r] for r in cited_refs if r in evidence] or list(evidence.values())
    citations = tuple(
        Citation(
            kind="document",
            source_id=str(item["source_id"]),
            title=item.get("title"),
            heading=item.get("heading") or None,
            breadcrumb=item.get("breadcrumb"),
            snippet=(str(item.get("content", ""))[:200] or None),
        )
        for item in used
    )
    answer = _SOURCES_RE.sub("", text).strip() or _REFUSAL
    return GroundedAnswer(answer=answer, citations=citations)


class NoEvidenceError(RuntimeError):
    """Raised when the team produced no citable evidence for the question."""


def _parse_cited_refs(text: str) -> list[int]:
    match = _SOURCES_RE.search(text)
    if not match:
        return []
    return [int(m) for m in _REF_RE.findall(match.group(1))]
