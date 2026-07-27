"""Offline evaluation contracts for the Data Bridge golden set."""

from databridge.evals.evaluator import EvaluationResult, EvaluationStatus, evaluate
from databridge.evals.observation import (
    CitationSnapshot,
    DocumentEvidenceSnapshot,
    Observation,
    RefusalDiagnosticsSnapshot,
    TraceSnapshot,
)
from databridge.evals.schema import GoldenItem, GoldenSet, load_golden
from databridge.evals.space import GoldenSpaceError, configure_golden_space

__all__ = [
    "CitationSnapshot",
    "DocumentEvidenceSnapshot",
    "EvaluationResult",
    "EvaluationStatus",
    "GoldenItem",
    "GoldenSet",
    "GoldenSpaceError",
    "Observation",
    "RefusalDiagnosticsSnapshot",
    "TraceSnapshot",
    "evaluate",
    "configure_golden_space",
    "load_golden",
]
