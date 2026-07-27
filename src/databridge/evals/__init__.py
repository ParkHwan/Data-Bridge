"""Offline evaluation contracts for the Data Bridge golden set."""

from databridge.evals.evaluator import EvaluationResult, EvaluationStatus, evaluate
from databridge.evals.observation import CitationSnapshot, Observation, TraceSnapshot
from databridge.evals.schema import GoldenItem, GoldenSet, load_golden
from databridge.evals.space import GoldenSpaceError, configure_golden_space

__all__ = [
    "CitationSnapshot",
    "EvaluationResult",
    "EvaluationStatus",
    "GoldenItem",
    "GoldenSet",
    "GoldenSpaceError",
    "Observation",
    "TraceSnapshot",
    "evaluate",
    "configure_golden_space",
    "load_golden",
]
