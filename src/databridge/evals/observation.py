"""Framework-independent snapshots consumed by the offline evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CitationKind = Literal["document", "bigquery", "report"]
TraceKind = Literal["tool_call", "tool_result", "final"]
Outcome = Literal["ok", "refusal", "error"]

_CITATION_KINDS = frozenset(("document", "bigquery", "report"))
_TRACE_KINDS = frozenset(("tool_call", "tool_result", "final"))


@dataclass(frozen=True, slots=True, kw_only=True)
class CitationSnapshot:
    """The citation fields selected for B+ evaluation."""

    kind: CitationKind
    source_id: str
    sql: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _CITATION_KINDS:
            raise ValueError(f"bad citation kind: {self.kind!r}")
        if not self.source_id.strip():
            raise ValueError("citation source_id must be non-empty")
        if self.kind == "bigquery" and not self.sql:
            raise ValueError("bigquery citation requires sql")


@dataclass(frozen=True, slots=True, kw_only=True)
class TraceSnapshot:
    """A runtime trace step; tool payloads are intentionally unavailable."""

    agent: str
    kind: TraceKind
    detail: str

    def __post_init__(self) -> None:
        if self.kind not in _TRACE_KINDS:
            raise ValueError(f"bad trace kind: {self.kind!r}")
        if not self.agent.strip():
            raise ValueError("trace agent must be non-empty")
        if not self.detail.strip():
            raise ValueError("trace detail must be non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class Observation:
    """One normalized live or fixture result at the selected observation surface."""

    outcome: Outcome
    answer: str = ""
    citations: tuple[CitationSnapshot, ...] = ()
    trace: tuple[TraceSnapshot, ...] = ()
    dropped_claims: tuple[str, ...] = ()
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.outcome == "ok":
            if not self.answer or not self.citations:
                raise ValueError("ok requires answer and citations")
            if self.error_type is not None or self.error_message is not None:
                raise ValueError("ok forbids error_type/error_message")
        elif self.outcome == "refusal":
            if self.answer or self.citations:
                raise ValueError("refusal forbids answer/citations")
            if self.error_type is not None:
                raise ValueError("refusal forbids error_type")
        elif self.outcome == "error":
            if not self.error_type:
                raise ValueError("error requires non-empty error_type")
            if self.answer or self.citations:
                raise ValueError("error forbids answer/citations")
        else:
            raise ValueError(f"bad outcome: {self.outcome!r}")
