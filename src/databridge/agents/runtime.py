"""Run the AI team and return a grounded result with a collaboration trace.

Citation policy (strict — post-review):
- Document evidence: every factual line must end with inline ``[n]`` markers naming
  the search_knowledge refs it used. Report-table rows put the markers in their final
  Source cell. Lines without resolving evidence are removed; if nothing grounded
  remains, the answer is refused.
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
_REF_RE = re.compile(r"\[(\d+)\]")
_SPACE_RUN_RE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCTUATION_RE = re.compile(r"[ \t]+([.,!?])")
_REF_SEPARATOR_RE = re.compile(r"[ \t]*,[ \t]*")
_TABLE_ROW_RE = re.compile(r"^(?P<prefix>\s*\|.*\|)(?P<cell>[^|]*)(?P<end>\|\s*)$")
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")
_HORIZONTAL_RULE_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_CODE_FENCE_RE = re.compile(r"^\s{0,3}`{3,}")

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
    dropped_claims: tuple[str, ...]

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

    grounded, dropped_claims = _ground_answer(final_text, doc_evidence, bq_evidence)
    return TeamResult(
        grounded=grounded,
        trace=tuple(trace),
        dropped_claims=dropped_claims,
    )


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
    """Build the public contract; retained for deterministic pure-function tests."""
    grounded, _ = _ground_answer(text, doc_evidence, bq_evidence)
    return grounded


def _ground_answer(
    text: str,
    doc_evidence: dict[int, dict[str, Any]],
    bq_evidence: list[dict[str, Any]],
) -> tuple[GroundedAnswer, tuple[str, ...]]:
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

    answer, document_citations, dropped_claims = _bind_claims(
        text,
        doc_evidence,
        # Only a true BigQuery-only response bypasses document-marker parsing. Mixed
        # responses still drop unmarked lines whenever document evidence is present.
        keep_unmarked=not doc_evidence,
    )
    citations.extend(document_citations)

    if not citations:
        raise NoEvidenceError(_REFUSAL)

    if not answer:
        raise NoEvidenceError(_REFUSAL)
    return GroundedAnswer(answer=answer, citations=tuple(citations)), dropped_claims


def _bind_claims(
    text: str,
    doc_evidence: dict[int, dict[str, Any]],
    *,
    keep_unmarked: bool = False,
) -> tuple[str, tuple[Citation, ...], tuple[str, ...]]:
    """Keep structural or grounded lines and bind resolving refs to citations."""
    lines = text.split("\n")
    structural = _structural_line_indexes(lines)
    kept: list[str] = []
    dropped: list[str] = []
    cited_refs: list[int] = []

    for index, line in enumerate(lines):
        if not line.strip() or index in structural:
            kept.append(line)
            continue

        table_match = _TABLE_ROW_RE.match(line)
        marker_target = table_match.group("cell") if table_match else line
        marker_matches = list(_REF_RE.finditer(marker_target))
        refs = [int(match.group(1)) for match in marker_matches]
        resolving = [ref for ref in refs if ref in doc_evidence]
        if resolving:
            clean_target = _remove_ref_markers(marker_target, marker_matches)
            kept.append(_rebuild_line(line, table_match, clean_target))
            cited_refs.extend(resolving)
        elif not refs and keep_unmarked:
            kept.append(line)
        else:
            dropped.append(line)

    unique_refs = list(dict.fromkeys(cited_refs))
    citations = tuple(_document_citation(doc_evidence[ref]) for ref in unique_refs)
    return "\n".join(kept).strip(), citations, tuple(dropped)


def _structural_line_indexes(lines: list[str]) -> set[int]:
    structural: set[int] = set()
    in_code_fence = False
    for index, line in enumerate(lines):
        is_fence = bool(_CODE_FENCE_RE.match(line))
        if in_code_fence or is_fence:
            structural.add(index)
        if is_fence:
            in_code_fence = not in_code_fence
            continue
        if _HEADING_RE.match(line) or _HORIZONTAL_RULE_RE.match(line):
            structural.add(index)
        if _TABLE_SEPARATOR_RE.match(line):
            structural.add(index)
            if index > 0 and _TABLE_ROW_RE.match(lines[index - 1]):
                structural.add(index - 1)
    return structural


def _rebuild_line(
    line: str,
    table_match: re.Match[str] | None,
    clean_target: str,
) -> str:
    if table_match:
        return f'{table_match.group("prefix")}{clean_target} {table_match.group("end")}'
    return clean_target


def _remove_ref_markers(text: str, matches: list[re.Match[str]]) -> str:
    """Remove ref spans and marker-only comma separators, then tidy visible text."""
    leading = text[: len(text) - len(text.lstrip(" \t"))]
    removal_spans = [match.span() for match in matches]
    for left, right in zip(matches, matches[1:], strict=False):
        between = text[left.end() : right.start()]
        if _REF_SEPARATOR_RE.fullmatch(between):
            removal_spans.append((left.end(), right.start()))

    cleaned = text
    for start, end in sorted(removal_spans, reverse=True):
        cleaned = cleaned[:start] + cleaned[end:]

    body = cleaned[len(leading) :].strip(" \t")
    body = _SPACE_RUN_RE.sub(" ", body)
    body = _SPACE_BEFORE_PUNCTUATION_RE.sub(r"\1", body)
    return leading + body


def _document_citation(item: dict[str, Any]) -> Citation:
    return Citation(
        kind="document",
        source_id=str(item["source_id"]),
        title=item.get("title"),
        heading=item.get("heading") or None,
        breadcrumb=item.get("breadcrumb"),
        snippet=(str(item.get("content", ""))[:200] or None),
    )
