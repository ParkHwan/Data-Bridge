"""BigQuery tools for the Data Agent — guarded NL2SQL execution (design D-7).

Guardrails, all enforced in code (never left to the model):
- read-only: statement must be a single SELECT (DML/DDL keywords rejected)
- allowlist: dry-run's referenced tables must all live in allowlisted datasets
- cost: dry-run estimate first, execution capped by maximum_bytes_billed
- volume: results truncated to a fixed row cap client-side
Every successful query is citable evidence: the exact SQL + referenced tables travel
back to the runtime, which turns them into a `bigquery` Citation.
"""

from __future__ import annotations

import os
import re
from typing import Any

_DEFAULT_DATASETS = "bigquery-public-data.thelook_ecommerce"
_MAX_BYTES_BILLED = 200 * 1024 * 1024  # 200 MB
_ROW_CAP = 50
_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|merge|create|drop|alter|truncate|grant|call|begin|commit)\b",
    re.IGNORECASE,
)


def _allowlisted_datasets() -> list[str]:
    raw = os.environ.get("DATABRIDGE_BQ_DATASETS", _DEFAULT_DATASETS)
    return [d.strip() for d in raw.split(",") if d.strip()]


def validate_sql(sql: str) -> str | None:
    """Static guard. Returns an error message or None if the statement may proceed."""
    stripped = re.sub(r"--[^\n]*", "", sql).strip().rstrip(";").strip()
    if not stripped.lower().startswith(("select", "with")):
        return "only single SELECT statements are allowed"
    if ";" in stripped:
        return "multiple statements are not allowed"
    if _FORBIDDEN_RE.search(stripped):
        return "read-only: DML/DDL keywords are not allowed"
    return None


def list_tables() -> list[dict[str, Any]]:
    """List queryable tables (allowlisted datasets) with their schemas.

    Call this first to ground your SQL in real table and column names.
    """
    from google.cloud import bigquery

    client = bigquery.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    out: list[dict[str, Any]] = []
    for dataset in _allowlisted_datasets():
        for table in client.list_tables(dataset):
            full = client.get_table(table.reference)
            out.append(
                {
                    "table": f"{full.project}.{full.dataset_id}.{full.table_id}",
                    "columns": [f"{f.name}:{f.field_type}" for f in full.schema],
                    "rows": full.num_rows,
                }
            )
    return out


def query_bigquery(sql: str) -> dict[str, Any]:
    """Run a read-only SELECT against allowlisted BigQuery datasets.

    Returns rows (capped) plus the exact SQL and referenced tables for citation.
    On guard violation returns {"error": ...} — fix the SQL and retry.
    """
    from google.cloud import bigquery

    guard_error = validate_sql(sql)
    if guard_error:
        return {"error": guard_error}

    client = bigquery.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    allow = _allowlisted_datasets()

    # Dry run: cost estimate + referenced-table allowlist check (design D-7).
    dry = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True))
    referenced = [
        f"{t.project}.{t.dataset_id}.{t.table_id}" for t in (dry.referenced_tables or [])
    ]
    for table in referenced:
        if not any(table.startswith(f"{d}.") for d in allow):
            return {"error": f"table {table} is outside allowlisted datasets {allow}"}

    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(maximum_bytes_billed=_MAX_BYTES_BILLED),
    )
    rows = [dict(row) for _, row in zip(range(_ROW_CAP), job.result(), strict=False)]
    return {
        "sql": sql,
        "referenced_tables": referenced,
        "estimated_bytes": dry.total_bytes_processed,
        "rows": [{k: str(v) for k, v in r.items()} for r in rows],
        "row_count_returned": len(rows),
    }
