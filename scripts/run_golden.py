"""Run the mini golden set against the live AI team (real Gemini + real store).

Usage:
    docker compose up -d && <ingest first>
    GOOGLE_CLOUD_PROJECT=genaiacademy-ph uv run python scripts/run_golden.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")

from databridge.agents.runtime import ask  # noqa: E402


def keyword_hit(answer: str, expected: list) -> float:
    if not expected:
        return 0.0
    low = answer.lower()
    hits = 0
    for entry in expected:
        aliases = entry if isinstance(entry, list) else [entry]
        if any(str(a).lower() in low for a in aliases):
            hits += 1
    return hits / len(expected)


def main() -> int:
    golden_path = Path(__file__).parents[1] / "evals" / "demo_golden.yaml"
    items = yaml.safe_load(golden_path.read_text(encoding="utf-8"))["items"]

    total_kw = 0.0
    source_hits = 0
    for item in items:
        result = ask(item["question"])
        kw = keyword_hit(result.answer, item["expected_keywords"])
        src = any(c.source_id == item["expected_source_id"] for c in result.citations)
        total_kw += kw
        source_hits += int(src)
        status = "PASS" if src and kw > 0 else "MISS"
        print(f"[{status}] {item['id']} kw={kw:.2f} src={'O' if src else 'X'}")
        print(f"    A: {result.answer[:140]}")
        print(f"    C: {[c.source_id for c in result.citations]}")

    n = len(items)
    print(f"\nsummary: keyword_hit={total_kw / n:.3f}  source_hit={source_hits}/{n}")
    return 0 if source_hits == n else 1


if __name__ == "__main__":
    sys.exit(main())
