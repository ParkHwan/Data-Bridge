"""Citation contract (design D-12).

One normalized citation object shared by every agent and the UI. The product rule is
"grounded or nothing": any claim an agent makes must carry at least one Citation.
The contract is framework-agnostic on purpose (design §4.1) — it must survive an agent
framework swap unchanged.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CitationKind = Literal["document", "bigquery", "report"]

_FQ_TABLE_RE = re.compile(r"^[\w-]+\.[\w$]+\.[\w$]+$")


class Citation(BaseModel):
    """A single verifiable pointer to where a claim came from.

    - ``document``: a chunk of an ingested document — requires ``source_id``;
      ``heading``/``breadcrumb`` locate the claim inside the document.
    - ``bigquery``: a live query result — requires ``sql`` (the exact statement run),
      ``source_id`` holds the fully-qualified table(s).
    - ``report``: a section of a generated report that itself carries citations —
      ``source_id`` is the report id; used when reports cite other reports.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CitationKind
    source_id: str = Field(min_length=1, description="document id / FQ table / report id")
    title: str | None = Field(default=None, description="human-readable source title")
    heading: str | None = Field(default=None, description="section heading (document)")
    breadcrumb: str | None = Field(
        default=None, description="hierarchy path, e.g. 'Handbook > Ops > Runbook'"
    )
    sql: str | None = Field(default=None, description="exact SQL executed (bigquery)")
    snippet: str | None = Field(default=None, description="short quoted evidence")

    @model_validator(mode="after")
    def _kind_requirements(self) -> Citation:
        if self.kind == "bigquery":
            if not self.sql:
                msg = "bigquery citation requires the exact SQL statement"
                raise ValueError(msg)
            if not _FQ_TABLE_RE.match(self.source_id):
                msg = "bigquery source_id must be a fully-qualified project.dataset.table"
                raise ValueError(msg)
        if self.kind == "document" and not (self.heading or self.snippet):
            # D-12: a document citation must be locatable inside the document —
            # source_id alone is not verifiable evidence.
            msg = "document citation requires heading or snippet"
            raise ValueError(msg)
        return self


class GroundedAnswer(BaseModel):
    """The only shape agents are allowed to return user-facing content in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str = Field(min_length=1)
    citations: tuple[Citation, ...] = Field(min_length=1)

    @property
    def citation_count(self) -> int:
        return len(self.citations)
