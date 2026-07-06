from __future__ import annotations

import pytest
from pydantic import ValidationError

from databridge.citations import Citation, GroundedAnswer


def test_document_citation_minimal() -> None:
    c = Citation(kind="document", source_id="doc-1", heading="Rollback procedure")
    assert c.source_id == "doc-1"


def test_bigquery_citation_requires_sql() -> None:
    with pytest.raises(ValidationError, match="SQL"):
        Citation(kind="bigquery", source_id="proj.ds.table")


def test_bigquery_citation_with_sql_ok() -> None:
    c = Citation(kind="bigquery", source_id="proj.ds.table", sql="SELECT 1")
    assert c.sql == "SELECT 1"


def test_grounded_answer_requires_at_least_one_citation() -> None:
    with pytest.raises(ValidationError):
        GroundedAnswer(answer="claim without evidence", citations=())


def test_grounded_answer_ok() -> None:
    ga = GroundedAnswer(
        answer="Deploys happen Tuesdays and Thursdays.",
        citations=(Citation(kind="document", source_id="doc-ops-runbook"),),
    )
    assert ga.citation_count == 1
