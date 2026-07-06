"""Normalized document model + Markdown (frontmatter) parsing.

Contract (ported from the sibling design, re-implemented):
- Every source becomes Markdown with YAML frontmatter carrying at least
  ``source_id``, ``title``, ``space_key``; optionally ``breadcrumb`` — the document's
  hierarchy path ("A > B"). Hierarchy context measurably improves retrieval quality
  (sibling project: +13.3pt keyword-hit on a golden set), so ingest preserves it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_FRONTMATTER_DELIMITER = "---"


@dataclass(frozen=True, slots=True)
class SourceDocument:
    source_id: str
    title: str
    space_key: str
    body: str
    breadcrumb: str | None = None


class FrontmatterError(ValueError):
    """Raised when a Markdown file lacks the required frontmatter contract."""


def parse_markdown(text: str, *, fallback_source_id: str | None = None) -> SourceDocument:
    """Parse frontmatter Markdown into a SourceDocument.

    Required frontmatter keys: ``title``, ``space_key``; ``source_id`` may fall back to
    the caller-provided value (e.g., file stem). Unknown keys are ignored.
    """
    meta, body = _split_frontmatter(text)
    source_id = str(meta.get("source_id") or fallback_source_id or "").strip()
    title = str(meta.get("title") or "").strip()
    space_key = str(meta.get("space_key") or "").strip()
    if not source_id or not title or not space_key:
        msg = "frontmatter requires source_id (or fallback), title, space_key"
        raise FrontmatterError(msg)
    breadcrumb_raw = meta.get("breadcrumb")
    breadcrumb = str(breadcrumb_raw).strip() if breadcrumb_raw else None
    return SourceDocument(
        source_id=source_id,
        title=title,
        space_key=space_key,
        body=body.strip(),
        breadcrumb=breadcrumb,
    )


def load_markdown_file(path: Path) -> SourceDocument:
    return parse_markdown(path.read_text(encoding="utf-8"), fallback_source_id=path.stem)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    stripped = text.lstrip()
    if not stripped.startswith(_FRONTMATTER_DELIMITER):
        raise FrontmatterError("missing frontmatter block")
    _, _, rest = stripped.partition(_FRONTMATTER_DELIMITER)
    meta_raw, delim, body = rest.partition(f"\n{_FRONTMATTER_DELIMITER}")
    if not delim:
        raise FrontmatterError("unterminated frontmatter block")
    loaded = yaml.safe_load(meta_raw) or {}
    if not isinstance(loaded, dict):
        raise FrontmatterError("frontmatter must be a YAML mapping")
    return loaded, body.lstrip("\n")
