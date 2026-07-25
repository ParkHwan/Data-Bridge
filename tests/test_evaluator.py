"""Pure offline coverage for the v2 golden evaluation safety net."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from scripts import run_golden

from databridge.evals.evaluator import EvaluationStatus, _matching_table_rows, evaluate
from databridge.evals.observation import CitationSnapshot, Observation, TraceSnapshot
from databridge.evals.schema import GoldenItem, GoldenSchemaError, GoldenSet, load_golden

ROOT = Path(__file__).parents[1]


def _knowledge_item(**updates: Any) -> GoldenItem:
    raw: dict[str, Any] = {
        "id": "DG-101",
        "kind": "knowledge",
        "question": "When are deploys?",
        "expected_source_id": "doc-ops-runbook",
        "expected_keywords": [["Tuesday"], ["14:00"]],
        "min_keyword": 1.0,
        "expected_citation_kind": "document",
        "expected_final_agent": "knowledge_agent",
        "required_agents": ["knowledge_agent"],
        "expected_tools_by_agent": {"knowledge_agent": ["search_knowledge"]},
        "strict_tools": True,
    }
    raw.update(updates)
    return GoldenItem.model_validate(raw)


def _data_item(**updates: Any) -> GoldenItem:
    raw: dict[str, Any] = {
        "id": "DG-102",
        "kind": "data",
        "question": "How many statuses?",
        "expected_keywords": [["status"], ["distinct"]],
        "expected_citation_kind": "bigquery",
        "expected_final_agent": "data_agent",
        "required_agents": ["data_agent"],
        "expected_tools_by_agent": {
            "data_agent": ["list_tables", "query_bigquery"]
        },
        "strict_tools": True,
    }
    raw.update(updates)
    return GoldenItem.model_validate(raw)


def _refusal_item(**updates: Any) -> GoldenItem:
    raw: dict[str, Any] = {
        "id": "DG-103",
        "kind": "refusal",
        "question": "Unknown private fact?",
    }
    raw.update(updates)
    return GoldenItem.model_validate(raw)


def _document_citation(source_id: str = "doc-ops-runbook") -> CitationSnapshot:
    return CitationSnapshot(kind="document", source_id=source_id)


def _bigquery_citation() -> CitationSnapshot:
    return CitationSnapshot(kind="bigquery", source_id="project.dataset.table", sql="SELECT 1")


def _knowledge_trace(*, final_agent: str = "knowledge_agent") -> tuple[TraceSnapshot, ...]:
    return (
        TraceSnapshot(agent="knowledge_agent", kind="tool_call", detail="search_knowledge"),
        TraceSnapshot(agent="knowledge_agent", kind="tool_result", detail="search_knowledge"),
        TraceSnapshot(agent=final_agent, kind="final", detail="answer"),
    )


def _data_trace() -> tuple[TraceSnapshot, ...]:
    return (
        TraceSnapshot(agent="data_agent", kind="tool_call", detail="list_tables"),
        TraceSnapshot(agent="data_agent", kind="tool_result", detail="list_tables"),
        TraceSnapshot(agent="data_agent", kind="tool_call", detail="query_bigquery"),
        TraceSnapshot(agent="data_agent", kind="tool_result", detail="query_bigquery"),
        TraceSnapshot(agent="data_agent", kind="final", detail="answer"),
    )


def _knowledge_observation(**updates: Any) -> Observation:
    raw: dict[str, Any] = {
        "outcome": "ok",
        "answer": "Deploys happen Tuesday at 14:00 UTC.",
        "citations": (_document_citation(),),
        "trace": _knowledge_trace(),
    }
    raw.update(updates)
    return Observation(**raw)


def _data_observation(**updates: Any) -> Observation:
    raw: dict[str, Any] = {
        "outcome": "ok",
        "answer": "There are distinct status values.",
        "citations": (_bigquery_citation(),),
        "trace": _data_trace(),
    }
    raw.update(updates)
    return Observation(**raw)


def test_demo_golden_is_explicit_v2_with_seven_migrated_and_four_new_items() -> None:
    golden = load_golden(ROOT / "evals" / "demo_golden.yaml")
    assert golden.version == 2
    assert [item.id for item in golden.items] == [f"DG-{index:03d}" for index in range(1, 12)]
    assert [item.kind for item in golden.items[7:]] == ["data", "data", "report", "refusal"]


def test_migrated_seven_items_pass_equivalent_offline_fixtures() -> None:
    golden = load_golden(ROOT / "evals" / "demo_golden.yaml")
    for loaded in golden.items[:7]:
        raw = loaded.model_dump()
        raw["expected_final_agent"] = "knowledge_agent"
        item = GoldenItem.model_validate(raw)
        assert item.expected_keywords is not None
        answer = " ".join(group[0] for group in item.expected_keywords)
        observation = Observation(
            outcome="ok",
            answer=answer,
            citations=(_document_citation(next(iter(item.expected_sources))),),
            trace=_knowledge_trace(),
        )
        assert evaluate(item, observation).status == EvaluationStatus.PASS


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"outcome": "ok"}, "ok requires"),
        (
            {
                "outcome": "ok",
                "answer": "answer",
                "citations": (_document_citation(),),
                "error_message": "impossible",
            },
            "ok forbids",
        ),
        ({"outcome": "refusal", "answer": "answer"}, "refusal forbids"),
        ({"outcome": "refusal", "error_type": "NoEvidenceError"}, "refusal forbids"),
        ({"outcome": "error", "error_type": ""}, "non-empty error_type"),
        (
            {
                "outcome": "error",
                "error_type": "RuntimeError",
                "answer": "answer",
            },
            "error forbids",
        ),
        ({"outcome": "invalid"}, "bad outcome"),
    ],
)
def test_observation_state_invariants(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Observation(**kwargs)


def test_refusal_preserves_optional_message() -> None:
    observation = Observation(outcome="refusal", error_message="grounded or nothing")
    assert observation.error_message == "grounded or nothing"


def test_trace_and_citation_snapshot_invariants() -> None:
    with pytest.raises(ValueError, match="trace kind"):
        TraceSnapshot(agent="agent", kind="unknown", detail="x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires sql"):
        CitationSnapshot(kind="bigquery", source_id="p.d.t")


def test_status_matrix_covers_refusal_fail_and_error() -> None:
    refusal = _refusal_item()
    assert evaluate(refusal, Observation(outcome="refusal")).status == EvaluationStatus.REFUSAL_OK
    assert evaluate(refusal, _knowledge_observation()).status == EvaluationStatus.FAIL
    assert (
        evaluate(_knowledge_item(), Observation(outcome="refusal")).status
        == EvaluationStatus.FAIL
    )
    assert (
        evaluate(
            _knowledge_item(),
            Observation(outcome="error", error_type="TimeoutError", error_message="late"),
        ).status
        == EvaluationStatus.ERROR
    )


@pytest.mark.parametrize(
    ("updates", "failure_prefix"),
    [
        ({"citations": (_document_citation("wrong-source"),)}, "source_hit:"),
        ({"answer": "Tuesday only"}, "keyword_hit:"),
        (
            {
                "citations": (
                    _document_citation(),
                    CitationSnapshot(kind="report", source_id="report-1"),
                )
            },
            "citation_kind:",
        ),
        ({"trace": _knowledge_trace(final_agent="databridge_root")}, "final_agent:"),
        ({"trace": ()}, "required_agents:"),
        ({"dropped_claims": ("unsupported",)}, "dropped_claims:"),
    ],
)
def test_each_observable_axis_fails_independently(
    updates: dict[str, Any], failure_prefix: str
) -> None:
    item = _knowledge_item(max_dropped_claims=0)
    result = evaluate(item, _knowledge_observation(**updates))
    assert result.status == EvaluationStatus.FAIL
    assert any(failure.startswith(failure_prefix) for failure in result.failures)


def test_unresolved_d9_placeholder_is_never_a_false_green() -> None:
    item = _knowledge_item(expected_final_agent="TODO(D-9): live 표본으로 확정")
    result = evaluate(item, _knowledge_observation())
    assert result.status == EvaluationStatus.FAIL
    assert any("unresolved D-9" in failure for failure in result.failures)


def test_data_requires_completed_call_result_pairs_in_order() -> None:
    broken_trace = (
        TraceSnapshot(agent="data_agent", kind="tool_call", detail="list_tables"),
        TraceSnapshot(agent="data_agent", kind="tool_call", detail="query_bigquery"),
        TraceSnapshot(agent="data_agent", kind="tool_result", detail="list_tables"),
        TraceSnapshot(agent="data_agent", kind="tool_result", detail="query_bigquery"),
        TraceSnapshot(agent="data_agent", kind="final", detail="answer"),
    )
    result = evaluate(_data_item(), _data_observation(trace=broken_trace))
    assert result.status == EvaluationStatus.FAIL
    assert any("ordered call/result" in failure for failure in result.failures)


def test_strict_tools_rejects_an_unexpected_tool_name() -> None:
    trace = _knowledge_trace()[:-1] + (
        TraceSnapshot(agent="knowledge_agent", kind="tool_call", detail="extra_tool"),
        TraceSnapshot(agent="knowledge_agent", kind="tool_result", detail="extra_tool"),
        _knowledge_trace()[-1],
    )
    result = evaluate(_knowledge_item(), _knowledge_observation(trace=trace))
    assert any("expected call names" in failure for failure in result.failures)


def test_max_tool_calls_rejects_repeated_allowed_calls() -> None:
    repeated = _knowledge_trace()[:-1] * 2 + (_knowledge_trace()[-1],)
    item = _knowledge_item(max_tool_calls=1)
    result = evaluate(item, _knowledge_observation(trace=repeated))
    assert any("expected <= 1 calls" in failure for failure in result.failures)


def test_bigquery_tool_success_is_inferred_only_from_sql_citation() -> None:
    observation = Observation(
        outcome="ok",
        answer="There are distinct status values.",
        citations=(_document_citation(),),
        trace=_data_trace(),
    )
    result = evaluate(_data_item(), observation)
    assert any(
        "query_bigquery requires bigquery citation" in failure
        for failure in result.failures
    )


def test_exact_numeric_value_uses_token_boundaries() -> None:
    item = _data_item(expected_keywords=None, expected_exact_value=42)
    assert evaluate(item, _data_observation(answer="The result is 42.")).passed
    result = evaluate(item, _data_observation(answer="The result is 142."))
    assert any("exact_value" in failure for failure in result.failures)


def test_report_table_requires_exact_english_headers_and_a_data_row() -> None:
    item = GoldenItem.model_validate(
        {
            "id": "DG-104",
            "kind": "report",
            "question": "Create an action report",
            "expected_source_id": "doc-meeting-atlas-kickoff",
            "expected_citation_kind": "document",
            "expected_final_agent": "report_agent",
            "required_agents": ["report_agent"],
            "expected_tools_by_agent": {"report_agent": ["search_knowledge"]},
            "expected_table_headers": ["Owner", "Action", "Due", "Source"],
            "min_table_rows": 1,
        }
    )
    trace = (
        TraceSnapshot(agent="report_agent", kind="tool_call", detail="search_knowledge"),
        TraceSnapshot(agent="report_agent", kind="tool_result", detail="search_knowledge"),
        TraceSnapshot(agent="report_agent", kind="final", detail="report"),
    )
    answer = "\n".join(
        [
            "| owner | ACTION | due | source |",
            "| --- | --- | --- | --- |",
            "| Jae | Draft design | 2026-06-22 | kickoff |",
        ]
    )
    observation = Observation(
        outcome="ok",
        answer=answer,
        citations=(_document_citation("doc-meeting-atlas-kickoff"),),
        trace=trace,
    )
    assert evaluate(item, observation).status == EvaluationStatus.PASS
    broken = Observation(
        outcome="ok",
        answer=answer.replace("source", "evidence"),
        citations=observation.citations,
        trace=trace,
    )
    assert evaluate(item, broken).status == EvaluationStatus.FAIL
    broken_separator = Observation(
        outcome="ok",
        answer=answer.replace("| --- | --- | --- | --- |", "| --- | --- |  | --- |"),
        citations=observation.citations,
        trace=trace,
    )
    assert evaluate(item, broken_separator).status == EvaluationStatus.FAIL


def test_report_table_counts_a_data_row_with_an_empty_source_cell() -> None:
    item = GoldenItem.model_validate(
        {
            "id": "DG-105",
            "kind": "report",
            "question": "Create an action report",
            "expected_source_id": "doc-meeting-atlas-kickoff",
            "expected_citation_kind": "document",
            "expected_final_agent": "report_agent",
            "required_agents": ["report_agent"],
            "expected_tools_by_agent": {"report_agent": ["search_knowledge"]},
            "expected_table_headers": ["Owner", "Action", "Due", "Source"],
            "min_table_rows": 1,
        }
    )
    answer = "\n".join(
        [
            "| Owner | Action | Due | Source |",
            "|---|---|---|---|",
            "| Jae | draft the Collector dual-write design | 2026-06-22 |  |",
        ]
    )
    observation = Observation(
        outcome="ok",
        answer=answer,
        citations=(_document_citation("doc-meeting-atlas-kickoff"),),
        trace=(
            TraceSnapshot(agent="report_agent", kind="tool_call", detail="search_knowledge"),
            TraceSnapshot(agent="report_agent", kind="tool_result", detail="search_knowledge"),
            TraceSnapshot(agent="report_agent", kind="final", detail="report"),
        ),
    )
    assert evaluate(item, observation).status == EvaluationStatus.PASS


def test_report_table_does_not_count_a_completely_empty_row() -> None:
    answer = "\n".join(
        [
            "| Owner | Action | Due | Source |",
            "|---|---|---|---|",
            "| | | | |",
        ]
    )
    assert _matching_table_rows(answer, ("Owner", "Action", "Due", "Source")) == 0


def test_report_table_uses_a_later_valid_table_after_an_empty_table() -> None:
    answer = "\n".join(
        [
            "| Owner | Action | Due | Source |",
            "|---|---|---|---|",
            "",
            "| Owner | Action | Due | Source |",
            "|---|---|---|---|",
            "| Jae | Draft design | 2026-06-22 | |",
        ]
    )
    assert _matching_table_rows(answer, ("Owner", "Action", "Due", "Source")) == 1


def test_run_item_converts_an_evaluator_exception_to_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = _knowledge_observation()

    async def fake_observe(question: str, *, item_timeout: float) -> Observation:
        assert question
        assert item_timeout == 1.0
        return observation

    def broken_evaluator(item: GoldenItem, actual: Observation) -> None:
        assert item.id == "DG-101"
        assert actual is observation
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(run_golden, "_observe", fake_observe)
    monkeypatch.setattr(run_golden, "evaluate", broken_evaluator)
    result = asyncio.run(run_golden._run_item(_knowledge_item(), item_timeout=1.0))

    assert result.evaluation.status == EvaluationStatus.ERROR
    assert result.evaluation.failures == ("evaluator: RuntimeError: parser exploded",)


@pytest.mark.parametrize(
    "mutation",
    [
        {"unknown": True},
        {"id": "bad-id"},
        {"expected_keywords": [[""]]},
        {"expected_source_ids": ["doc-ops-runbook"]},
    ],
)
def test_schema_rejects_unknown_bad_id_empty_alias_and_source_xor(
    mutation: dict[str, Any]
) -> None:
    raw = _knowledge_item().model_dump()
    raw.update(mutation)
    with pytest.raises(ValidationError):
        GoldenItem.model_validate(raw)


@pytest.mark.parametrize(
    "field",
    [
        "expected_final_agent",
        "required_agents",
        "expected_tools_by_agent",
        "strict_tools",
        "max_tool_calls",
        "max_dropped_claims",
        "expected_keywords",
        "min_keyword",
    ],
)
def test_refusal_rejects_positive_and_unobservable_fields(field: str) -> None:
    values: dict[str, Any] = {
        "expected_final_agent": "knowledge_agent",
        "required_agents": ["knowledge_agent"],
        "expected_tools_by_agent": {"knowledge_agent": ["search_knowledge"]},
        "strict_tools": False,
        "max_tool_calls": 1,
        "max_dropped_claims": 0,
        "expected_keywords": [["no"]],
        "min_keyword": 0.5,
    }
    with pytest.raises(ValidationError, match="refusal item forbids"):
        _refusal_item(**{field: values[field]})


def test_schema_rejects_bool_version_duplicate_ids_and_empty_items() -> None:
    item = _refusal_item()
    with pytest.raises(ValidationError, match="integer 2"):
        GoldenSet.model_validate({"version": True, "items": [item]})
    with pytest.raises(ValidationError, match="unique"):
        GoldenSet(version=2, items=(item, item))
    with pytest.raises(ValidationError, match="non-empty"):
        GoldenSet(version=2, items=())


def test_data_schema_requires_an_answer_assertion() -> None:
    with pytest.raises(ValidationError, match="exact value or keywords"):
        _data_item(expected_keywords=None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("strict_tools", "false"),
        ("max_tool_calls", True),
        ("max_dropped_claims", False),
        ("min_keyword", "1.0"),
        ("min_table_rows", True),
    ],
)
def test_schema_rejects_coerced_scalar_types(field: str, value: object) -> None:
    raw = _knowledge_item().model_dump()
    raw[field] = value
    with pytest.raises(ValidationError):
        GoldenItem.model_validate(raw)


@pytest.mark.parametrize(
    "fields",
    [
        {"min_table_rows": 1},
        {"expected_table_headers": ["Owner", "Action", "Due", "Source"]},
    ],
)
def test_schema_rejects_table_assertions_outside_report(fields: dict[str, object]) -> None:
    raw = _knowledge_item().model_dump()
    raw.update(fields)
    with pytest.raises(ValidationError):
        GoldenItem.model_validate(raw)


def test_loader_wraps_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_bytes(b"\xff")
    with pytest.raises(GoldenSchemaError, match="invalid golden file"):
        load_golden(path)
