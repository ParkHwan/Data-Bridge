"""Static SQL guard tests (pure function — no BigQuery client)."""

from __future__ import annotations

import pytest

from databridge.agents.tools_bigquery import validate_sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select count(*) from `bigquery-public-data.thelook_ecommerce.orders` limit 10",
        "WITH t AS (SELECT 1 AS a) SELECT a FROM t",
        "-- comment\nSELECT 1",
        "SELECT 1;",
    ],
)
def test_valid_selects_pass(sql: str) -> None:
    assert validate_sql(sql) is None


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("DROP TABLE x", "SELECT"),
        ("INSERT INTO x VALUES (1)", "SELECT"),
        ("SELECT 1; SELECT 2", "multiple"),
        ("SELECT * FROM x; DROP TABLE y", "multiple"),
        ("SELECT (SELECT delete FROM y) AS sneaky", "read-only"),
    ],
)
def test_forbidden_statements_rejected(sql: str, fragment: str) -> None:
    error = validate_sql(sql)
    assert error is not None and fragment.lower() in error.lower()
