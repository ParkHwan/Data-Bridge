from __future__ import annotations

import pytest

from databridge.ingest.chunker import chunk_document
from databridge.ingest.markdown import FrontmatterError, SourceDocument, parse_markdown

SAMPLE = """---
source_id: "d1"
title: "Doc One"
space_key: "DEMO"
breadcrumb: "A > B"
---

intro line before any heading

## Section One

body one

## Section Two

body two
"""


def test_parse_markdown_frontmatter() -> None:
    doc = parse_markdown(SAMPLE)
    assert doc.source_id == "d1"
    assert doc.breadcrumb == "A > B"
    assert doc.body.startswith("intro line")


def test_parse_markdown_missing_required_raises() -> None:
    with pytest.raises(FrontmatterError):
        parse_markdown("---\ntitle: x\n---\nbody")


def test_chunks_follow_headings_and_carry_context() -> None:
    doc = parse_markdown(SAMPLE)
    chunks = chunk_document(doc)
    headings = [c.heading for c in chunks]
    assert headings == [None, "Section One", "Section Two"]
    assert all(c.breadcrumb == "A > B" for c in chunks)
    assert chunks[1].embedding_text.startswith("[A > B > Doc One > Section One]")
    # stored content stays clean (no context header)
    assert not chunks[1].content.startswith("[")


def test_oversized_section_splits_with_unique_ids() -> None:
    body = "## Big\n" + "\n".join(f"line {i} " + "x" * 80 for i in range(60))
    doc = SourceDocument(source_id="d2", title="Big Doc", space_key="DEMO", body=body)
    chunks = chunk_document(doc, max_chars=1000)
    assert len(chunks) > 1
    assert len({c.chunk_id for c in chunks}) == len(chunks)
    assert all(c.heading == "Big" for c in chunks)


def test_nested_headings_keep_parent_path() -> None:
    body = "## API\nintro\n### Retry\napi retry\n## Worker\nintro\n### Retry\nworker retry"
    doc = SourceDocument(source_id="d3", title="Doc", space_key="DEMO", body=body)
    headings = [c.heading for c in chunk_document(doc)]
    assert "API > Retry" in headings
    assert "Worker > Retry" in headings


def test_heading_inside_code_fence_is_not_a_section() -> None:
    body = "## Real\ntext\n```\n## not a heading\n```\nmore text"
    doc = SourceDocument(source_id="d4", title="Doc", space_key="DEMO", body=body)
    chunks = chunk_document(doc)
    assert [c.heading for c in chunks] == ["Real"]
    assert "## not a heading" in chunks[0].content


def test_heading_only_section_is_dropped() -> None:
    body = "## Empty Section\n\n## Full\ncontent here"
    doc = SourceDocument(source_id="d5", title="Doc", space_key="DEMO", body=body)
    assert [c.heading for c in chunk_document(doc)] == ["Full"]
