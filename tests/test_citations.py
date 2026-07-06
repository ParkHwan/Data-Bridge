from __future__ import annotations

import pytest
from pydantic import ValidationError

from databridge.citations import Citation, GroundedAnswer


def test_document_citation_requires_locating_field() -> None:
    # D-12: source_id alone is not verifiable evidence (post-review P1).
    with pytest.raises(ValidationError, match="heading or snippet"):
        Citation(kind="document", source_id="doc-1")


def test_document_citation_with_heading_ok() -> None:
    c = Citation(kind="document", source_id="doc-1", heading="Rollback procedure")
    assert c.heading == "Rollback procedure"


def test_document_citation_with_snippet_ok() -> None:
    c = Citation(kind="document", source_id="doc-1", snippet="roll back by re-applying")
    assert c.snippet is not None


def test_bigquery_citation_requires_sql() -> None:
    with pytest.raises(ValidationError, match="SQL"):
        Citation(kind="bigquery", source_id="proj.ds.table")


def test_bigquery_citation_requires_fq_table() -> None:
    with pytest.raises(ValidationError, match="fully-qualified"):
        Citation(kind="bigquery", source_id="just_a_table", sql="SELECT 1")


def test_bigquery_citation_ok() -> None:
    c = Citation(kind="bigquery", source_id="proj.ds.table", sql="SELECT 1")
    assert c.sql == "SELECT 1"


def test_grounded_answer_requires_at_least_one_citation() -> None:
    with pytest.raises(ValidationError):
        GroundedAnswer(answer="claim without evidence", citations=())


def test_grounded_answer_ok() -> None:
    ga = GroundedAnswer(
        answer="Deploys happen Tuesdays and Thursdays.",
        citations=(
            Citation(kind="document", source_id="doc-ops-runbook", heading="Release cadence"),
        ),
    )
    assert ga.citation_count == 1
