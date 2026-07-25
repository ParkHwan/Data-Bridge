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
    Observation,
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
