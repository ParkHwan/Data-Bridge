"""Pure evaluation of a validated golden item against an Observation fixture."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from databridge.evals.observation import Observation, TraceSnapshot
from databridge.evals.schema import D9_FINAL_AGENT_TODO, GoldenItem


class EvaluationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    REFUSAL_OK = "REFUSAL_OK"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    item_id: str
    status: EvaluationStatus
    failures: tuple[str, ...] = ()
    keyword_hit: float | None = None

    @property
    def passed(self) -> bool:
        return self.status in {EvaluationStatus.PASS, EvaluationStatus.REFUSAL_OK}


def evaluate(item: GoldenItem, observation: Observation) -> EvaluationResult:
    """Evaluate one item without importing runtime, ADK, or GCP dependencies."""

    if observation.outcome == "error":
        detail = observation.error_type or "unknown error"
        if observation.error_message:
            detail = f"{detail}: {observation.error_message}"
        return EvaluationResult(
            item_id=item.id,
            status=EvaluationStatus.ERROR,
            failures=(f"execution: {detail}",),
        )

    if item.kind == "refusal":
        if observation.outcome == "refusal":
            if (
                observation.refusal_diagnostics is not None
                and not _has_completed_tool(observation.trace, "search_knowledge")
            ):
                return EvaluationResult(
                    item_id=item.id,
                    status=EvaluationStatus.FAIL,
                    failures=(
                        "refusal_process: search_knowledge was not completed before refusal",
                    ),
                )
            return EvaluationResult(item_id=item.id, status=EvaluationStatus.REFUSAL_OK)
        return EvaluationResult(
            item_id=item.id,
            status=EvaluationStatus.FAIL,
            failures=("refusal: expected NoEvidenceError, got a grounded answer",),
        )

    if observation.outcome == "refusal":
        return EvaluationResult(
            item_id=item.id,
            status=EvaluationStatus.FAIL,
            failures=("refusal: unexpected NoEvidenceError for a non-refusal item",),
        )

    failures: list[str] = []
    keyword_score = _evaluate_keywords(item, observation, failures)
    _evaluate_sources(item, observation, failures)
    _evaluate_citation_kind(item, observation, failures)
    _evaluate_final_agent(item, observation, failures)
    _evaluate_required_agents(item, observation, failures)
    _evaluate_tools(item, observation, failures)
    _evaluate_exact_value(item, observation, failures)
    _evaluate_report_table(item, observation, failures)
    if (
        item.max_dropped_claims is not None
        and len(observation.dropped_claims) > item.max_dropped_claims
    ):
        failures.append(
            "dropped_claims: "
            f"expected <= {item.max_dropped_claims}, got {len(observation.dropped_claims)}"
        )

    return EvaluationResult(
        item_id=item.id,
        status=EvaluationStatus.FAIL if failures else EvaluationStatus.PASS,
        failures=tuple(failures),
        keyword_hit=keyword_score,
    )


def keyword_hit(answer: str, expected: tuple[tuple[str, ...], ...]) -> float:
    """Return the fraction of OR-groups with at least one case-insensitive match."""

    if not expected:
        return 0.0
    folded = answer.casefold()
    hits = sum(any(alias.casefold() in folded for alias in group) for group in expected)
    return hits / len(expected)


def _evaluate_keywords(
    item: GoldenItem, observation: Observation, failures: list[str]
) -> float | None:
    if item.expected_keywords is None:
        return None
    score = keyword_hit(observation.answer, item.expected_keywords)
    if score < item.min_keyword:
        failures.append(
            f"keyword_hit: expected >= {item.min_keyword:.3f}, got {score:.3f}"
        )
    return score


def _evaluate_sources(
    item: GoldenItem, observation: Observation, failures: list[str]
) -> None:
    expected = item.expected_sources
    if not expected:
        return
    actual = {citation.source_id for citation in observation.citations}
    missing = sorted(expected - actual)
    if missing:
        failures.append(f"source_hit: missing {missing}; actual={sorted(actual)}")


def _evaluate_citation_kind(
    item: GoldenItem, observation: Observation, failures: list[str]
) -> None:
    expected = item.expected_citation_kind
    if expected is None:
        return
    actual = [citation.kind for citation in observation.citations]
    if not actual or any(kind != expected for kind in actual):
        failures.append(f"citation_kind: expected all {expected}; actual={actual}")


def _evaluate_final_agent(
    item: GoldenItem, observation: Observation, failures: list[str]
) -> None:
    expected = item.expected_final_agent
    if expected is None:
        return
    finals = [step.agent for step in observation.trace if step.kind == "final"]
    actual = finals[-1] if finals else None
    if expected == D9_FINAL_AGENT_TODO:
        failures.append(f"final_agent: unresolved D-9 placeholder; observed={actual!r}")
    elif actual != expected:
        failures.append(f"final_agent: expected {expected!r}, got {actual!r}")


def _evaluate_required_agents(
    item: GoldenItem, observation: Observation, failures: list[str]
) -> None:
    actual = {step.agent for step in observation.trace}
    missing = sorted(set(item.required_agents) - actual)
    if missing:
        failures.append(f"required_agents: missing {missing}; actual={sorted(actual)}")


def _evaluate_tools(item: GoldenItem, observation: Observation, failures: list[str]) -> None:
    expected_by_agent = item.expected_tools_by_agent
    if expected_by_agent is None:
        return
    for agent, expected in expected_by_agent.items():
        agent_steps = [step for step in observation.trace if step.agent == agent]
        actual_calls = [step.detail for step in agent_steps if step.kind == "tool_call"]
        if item.strict_tools and set(actual_calls) != set(expected):
            failures.append(
                f"tools[{agent}]: expected call names {sorted(set(expected))}, "
                f"actual={sorted(set(actual_calls))}"
            )
        if item.max_tool_calls is not None and len(actual_calls) > item.max_tool_calls:
            failures.append(
                f"tools[{agent}]: expected <= {item.max_tool_calls} calls, got {len(actual_calls)}"
            )
        missing_pair = _missing_ordered_pair(agent_steps, expected)
        if missing_pair is not None:
            failures.append(
                f"tools[{agent}]: missing ordered call/result subsequence for {missing_pair}"
            )

    expected_tools = {
        tool for tools in expected_by_agent.values() for tool in tools
    }
    if "query_bigquery" in expected_tools and not any(
        citation.kind == "bigquery" and bool(citation.sql)
        for citation in observation.citations
    ):
        failures.append("tool_evidence: query_bigquery requires bigquery citation with sql")
    if "search_knowledge" in expected_tools and not any(
        citation.kind == "document" for citation in observation.citations
    ):
        failures.append("tool_evidence: search_knowledge requires document citation")


def _missing_ordered_pair(
    steps: list[TraceSnapshot], expected_tools: tuple[str, ...]
) -> str | None:
    cursor = 0
    for tool in expected_tools:
        call_index = _find_step(steps, cursor, kind="tool_call", detail=tool)
        if call_index is None:
            return tool
        result_index = _find_step(steps, call_index + 1, kind="tool_result", detail=tool)
        if result_index is None:
            return tool
        cursor = result_index + 1
    return None


def _find_step(
    steps: list[TraceSnapshot], start: int, *, kind: str, detail: str
) -> int | None:
    for index in range(start, len(steps)):
        step = steps[index]
        if step.kind == kind and step.detail == detail:
            return index
    return None


def _has_completed_tool(steps: tuple[TraceSnapshot, ...], tool: str) -> bool:
    step_list = list(steps)
    call_index = _find_step(step_list, 0, kind="tool_call", detail=tool)
    return call_index is not None and _find_step(
        step_list, call_index + 1, kind="tool_result", detail=tool
    ) is not None


def _evaluate_exact_value(
    item: GoldenItem, observation: Observation, failures: list[str]
) -> None:
    expected = item.expected_exact_value
    if expected is None:
        return
    token = str(expected).strip()
    if isinstance(expected, (int, float)):
        pattern = rf"(?<![\w.]){re.escape(token)}(?!\w|[.,]\d)"
        matched = re.search(pattern, observation.answer) is not None
    else:
        matched = token.casefold() in observation.answer.casefold()
    if not matched:
        failures.append(f"exact_value: expected token {token!r} in answer")


def _evaluate_report_table(
    item: GoldenItem, observation: Observation, failures: list[str]
) -> None:
    expected_headers = item.expected_table_headers
    if expected_headers is None:
        return
    rows = _matching_table_rows(observation.answer, expected_headers)
    minimum = item.min_table_rows or 1
    if rows is None:
        failures.append(f"report_table: expected headers {'|'.join(expected_headers)}")
    elif rows < minimum:
        failures.append(f"report_table: expected >= {minimum} data rows, got {rows}")


def _matching_table_rows(answer: str, expected_headers: tuple[str, ...]) -> int | None:
    lines = answer.splitlines()
    normalized_expected = tuple(header.strip().casefold() for header in expected_headers)
    maximum_rows: int | None = None
    for index, line in enumerate(lines[:-1]):
        cells = _markdown_cells(line)
        if cells is None or tuple(cell.casefold() for cell in cells) != normalized_expected:
            continue
        separator = _markdown_cells(lines[index + 1])
        if separator is None or len(separator) != len(cells):
            continue
        if not all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in separator):
            continue
        row_count = 0
        for row in lines[index + 2 :]:
            row_cells = _markdown_cells(row, allow_empty=True)
            if row_cells is None or len(row_cells) != len(cells):
                break
            if not any(row_cells):
                continue
            row_count += 1
        maximum_rows = max(maximum_rows or 0, row_count)
    return maximum_rows


def _markdown_cells(line: str, *, allow_empty: bool = False) -> tuple[str, ...] | None:
    stripped = line.strip()
    if "|" not in stripped:
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = tuple(cell.strip() for cell in stripped.split("|"))
    return cells if cells and (allow_empty or all(cells)) else None
