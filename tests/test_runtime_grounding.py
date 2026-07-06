"""Unit tests for the strict grounding policy (no live LLM needed)."""

from __future__ import annotations

import pytest

from databridge.agents.runtime import (
    NoEvidenceError,
    _parse_cited_refs,
    _to_grounded_answer,
)

DOC = {
    1: {
        "ref": 1,
        "source_id": "doc-ops-runbook",
        "title": "Deployment Runbook",
        "heading": "Release cadence",
        "breadcrumb": "Handbook > Engineering > Operations",
        "content": "Production deploys happen every Tuesday and Thursday.",
    },
    2: {
        "ref": 2,
        "source_id": "doc-risk-log",
        "title": "Risk Log",
        "heading": "R-01 Dual-write drift",
        "breadcrumb": None,
        "content": "Severity: high.",
    },
}


def test_markers_map_to_document_citations() -> None:
    ga = _to_grounded_answer("Deploys are twice weekly.\nSOURCES: [1]", DOC, [])
    assert [c.source_id for c in ga.citations] == ["doc-ops-runbook"]
    assert "SOURCES" not in ga.answer


def test_no_markers_is_refused_not_padded() -> None:
    # Strict policy (post-review P1): missing markers must not silently cite
    # everything that was retrieved.
    with pytest.raises(NoEvidenceError):
        _to_grounded_answer("Confident but uncited claim.", DOC, [])


def test_markers_referencing_unknown_refs_are_refused() -> None:
    with pytest.raises(NoEvidenceError):
        _to_grounded_answer("Answer.\nSOURCES: [9]", DOC, [])


def test_no_evidence_at_all_is_refused() -> None:
    with pytest.raises(NoEvidenceError):
        _to_grounded_answer("Answer.\nSOURCES: [1]", {}, [])


def test_bigquery_evidence_cites_sql_automatically() -> None:
    bq = [
        {
            "sql": "SELECT count(*) FROM `bigquery-public-data.thelook_ecommerce.orders`",
            "referenced_tables": ["bigquery-public-data.thelook_ecommerce.orders"],
            "row_count_returned": 1,
        }
    ]
    ga = _to_grounded_answer("There are N orders.", {}, bq)
    assert ga.citations[0].kind == "bigquery"
    assert ga.citations[0].sql is not None and "SELECT" in ga.citations[0].sql


def test_mixed_evidence_combines_citations() -> None:
    bq = [
        {
            "sql": "SELECT 1",
            "referenced_tables": ["p.d.t"],
            "row_count_returned": 1,
        }
    ]
    ga = _to_grounded_answer("Combined.\nSOURCES: [2]", DOC, bq)
    kinds = sorted(c.kind for c in ga.citations)
    assert kinds == ["bigquery", "document"]


def test_parse_cited_refs_dedupes_and_orders() -> None:
    assert _parse_cited_refs("x SOURCES: [2][1] [2]") == [2, 1]
    assert _parse_cited_refs("no markers") == []
