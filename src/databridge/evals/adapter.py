"""Framework-independent adapters for live golden observations."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from databridge.evals.evaluator import EvaluationResult, EvaluationStatus, evaluate
from databridge.evals.observation import (
    CitationKind,
    CitationSnapshot,
    DocumentEvidenceSnapshot,
    Observation,
    RefusalDiagnosticsSnapshot,
    TraceKind,
    TraceSnapshot,
)
from databridge.evals.schema import GoldenItem


class _CitationLike(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def source_id(self) -> str: ...

    @property
    def sql(self) -> str | None: ...


class _TraceLike(Protocol):
    @property
    def agent(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def detail(self) -> str: ...


class _DocumentEvidenceLike(Protocol):
    @property
    def source_id(self) -> str: ...

    @property
    def heading(self) -> str | None: ...


class RefusalDiagnosticsLike(Protocol):
    @property
    def trace(self) -> Sequence[_TraceLike]: ...

    @property
    def documents(self) -> Sequence[_DocumentEvidenceLike]: ...

    @property
    def search_result_counts(self) -> Sequence[int | None]: ...

    @property
    def bq_evidence_count(self) -> int: ...

    @property
    def final_text_empty(self) -> bool: ...

    @property
    def final_text_length(self) -> int: ...

    @property
    def citation_count(self) -> int: ...

    @property
    def answer_empty(self) -> bool: ...

    @property
    def referenced_refs(self) -> Sequence[int]: ...

    @property
    def resolving_ref_count(self) -> int: ...


class TeamResultLike(Protocol):
    """The runtime result surface needed to build an offline observation."""

    @property
    def answer(self) -> str: ...

    @property
    def citations(self) -> Sequence[_CitationLike]: ...

    @property
    def trace(self) -> Sequence[_TraceLike]: ...

    @property
    def dropped_claims(self) -> Sequence[str]: ...


class Observer(Protocol):
    def __call__(
        self, question: str, *, item_timeout: float
    ) -> Awaitable[Observation]: ...


Evaluator = Callable[[GoldenItem, Observation], EvaluationResult]


@dataclass(frozen=True, slots=True)
class ItemRun:
    item: GoldenItem
    observation: Observation
    evaluation: EvaluationResult
    elapsed_seconds: float


def snapshot_result(result: TeamResultLike) -> Observation:
    """Select only evaluator-visible fields from a successful runtime result."""

    return Observation(
        outcome="ok",
        answer=result.answer,
        citations=tuple(
            CitationSnapshot(
                kind=cast(CitationKind, citation.kind),
                source_id=citation.source_id,
                sql=citation.sql,
            )
            for citation in result.citations
        ),
        trace=tuple(
            TraceSnapshot(
                agent=step.agent,
                kind=cast(TraceKind, step.kind),
                detail=step.detail,
            )
            for step in result.trace
        ),
        dropped_claims=tuple(result.dropped_claims),
    )


def snapshot_refusal(
    message: str, diagnostics: RefusalDiagnosticsLike | None
) -> Observation:
    """Preserve safe runtime diagnostics while keeping evals framework-independent."""
    if diagnostics is None:
        return Observation(outcome="refusal", error_message=message)
    return Observation(
        outcome="refusal",
        error_message=message,
        trace=tuple(
            TraceSnapshot(
                agent=step.agent,
                kind=cast(TraceKind, step.kind),
                detail=step.detail,
            )
            for step in diagnostics.trace
        ),
        refusal_diagnostics=RefusalDiagnosticsSnapshot(
            documents=tuple(
                DocumentEvidenceSnapshot(
                    source_id=document.source_id,
                    heading=document.heading,
                )
                for document in diagnostics.documents
            ),
            search_result_counts=tuple(diagnostics.search_result_counts),
            bq_evidence_count=diagnostics.bq_evidence_count,
            final_text_empty=diagnostics.final_text_empty,
            final_text_length=diagnostics.final_text_length,
            citation_count=diagnostics.citation_count,
            answer_empty=diagnostics.answer_empty,
            referenced_refs=tuple(diagnostics.referenced_refs),
            resolving_ref_count=diagnostics.resolving_ref_count,
        ),
    )


async def run_item(
    item: GoldenItem,
    *,
    item_timeout: float,
    observe: Observer,
    evaluator: Evaluator = evaluate,
) -> ItemRun:
    """Observe and evaluate one item without allowing evaluator faults to escape."""

    started = time.monotonic()
    observation = await observe(item.question, item_timeout=item_timeout)
    try:
        evaluation = evaluator(item, observation)
    except Exception as exc:
        detail = type(exc).__name__
        if str(exc):
            detail = f"{detail}: {exc}"
        evaluation = EvaluationResult(
            item_id=item.id,
            status=EvaluationStatus.ERROR,
            failures=(f"evaluator: {detail}",),
        )
    return ItemRun(
        item=item,
        observation=observation,
        evaluation=evaluation,
        elapsed_seconds=time.monotonic() - started,
    )
