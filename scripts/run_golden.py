"""Run a versioned golden set against the live team and evaluate observable contracts.

Usage:
    docker compose up -d && <ingest first>
    GOOGLE_CLOUD_PROJECT=genaiacademy-ph uv run python scripts/run_golden.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")

from databridge.agents.deps import get_deps  # noqa: E402
from databridge.agents.runtime import NoEvidenceError, ask_async  # noqa: E402
from databridge.evals.adapter import (  # noqa: E402
    ItemRun,
    run_item,
    snapshot_refusal,
    snapshot_result,
)
from databridge.evals.observation import Observation  # noqa: E402
from databridge.evals.schema import GoldenItem, GoldenSchemaError, load_golden  # noqa: E402
from databridge.evals.space import (  # noqa: E402
    GoldenSpaceError,
    configure_golden_space,
)


async def _observe(question: str, *, item_timeout: float) -> Observation:
    try:
        result = await asyncio.wait_for(ask_async(question), timeout=item_timeout)
        return snapshot_result(result)
    except NoEvidenceError as exc:
        return snapshot_refusal(str(exc), exc.diagnostics)
    except Exception as exc:
        return Observation(
            outcome="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


async def _run_item(item: GoldenItem, *, item_timeout: float) -> ItemRun:
    return await run_item(item, item_timeout=item_timeout, observe=_observe)


def _print_item(run: ItemRun) -> None:
    result = run.evaluation
    observation = run.observation
    calls = sum(step.kind == "tool_call" for step in observation.trace)
    finals = [step.agent for step in observation.trace if step.kind == "final"]
    final_agent = finals[-1] if finals else "-"
    keyword = "-" if result.keyword_hit is None else f"{result.keyword_hit:.3f}"
    print(
        f"[{result.status.value}] {run.item.id} "
        f"time={run.elapsed_seconds:.2f}s calls={calls} kw={keyword} final={final_agent}"
    )
    for failure in result.failures:
        print(f"    {failure}")
    if observation.answer:
        print(f"    A: {observation.answer[:140]}")
    if observation.citations:
        print(f"    C: {[citation.source_id for citation in observation.citations]}")
    if observation.outcome == "refusal" and observation.error_message:
        print(f"    R: {observation.error_message}")
        diagnostics = observation.refusal_diagnostics
        if diagnostics is not None:
            tools = [
                step.detail for step in observation.trace if step.kind == "tool_call"
            ]
            print(
                "    D: "
                f"tools={tools} search_results={list(diagnostics.search_result_counts)} "
                f"docs={len(diagnostics.documents)} bq={diagnostics.bq_evidence_count} "
                f"citations={diagnostics.citation_count} "
                f"answer_empty={diagnostics.answer_empty} "
                f"final_empty={diagnostics.final_text_empty} "
                f"final_len={diagnostics.final_text_length} "
                f"refs={list(diagnostics.referenced_refs)} "
                f"resolving_refs={diagnostics.resolving_ref_count}"
            )


async def _run_all(
    items: tuple[GoldenItem, ...], *, item_timeout: float
) -> tuple[ItemRun, ...]:
    runs: list[ItemRun] = []
    for item in items:
        run = await _run_item(item, item_timeout=item_timeout)
        runs.append(run)
        _print_item(run)
    return tuple(runs)


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).parents[1]
    parser = argparse.ArgumentParser(description="Run the live Data Bridge golden set")
    parser.add_argument("--golden", type=Path, default=root / "evals" / "demo_golden.yaml")
    parser.add_argument("--space", help="Assert the golden set's search space (never overrides it)")
    parser.add_argument("--item-timeout", type=float, default=120.0)
    parser.add_argument("--total-timeout", type=float, default=1200.0)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.item_timeout <= 0 or args.total_timeout <= 0:
        print("timeouts must be positive", file=sys.stderr)
        return 2
    try:
        golden = load_golden(args.golden)
    except GoldenSchemaError as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        actual_space = configure_golden_space(
            golden_space=golden.space_key,
            cli_space=args.space,
            get_actual_space=lambda: get_deps().space_key,
        )
    except GoldenSpaceError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"space: {actual_space}")

    started = time.monotonic()
    try:
        runs = asyncio.run(
            asyncio.wait_for(
                _run_all(golden.items, item_timeout=args.item_timeout),
                timeout=args.total_timeout,
            )
        )
    except TimeoutError:
        print(f"[ERROR] total timeout exceeded ({args.total_timeout:.1f}s)", file=sys.stderr)
        return 1

    passed = sum(run.evaluation.passed for run in runs)
    calls = sum(
        step.kind == "tool_call" for run in runs for step in run.observation.trace
    )
    elapsed = time.monotonic() - started
    print(
        f"\nsummary: passed={passed}/{len(runs)} calls={calls} elapsed={elapsed:.2f}s"
    )
    return 0 if passed == len(runs) else 1


if __name__ == "__main__":
    sys.exit(main())
