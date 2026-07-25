"""Convert Confluence API pages into the repository's ingest contract."""

from __future__ import annotations

from databridge.confluence.exceptions import EmptyBodyError
from databridge.confluence.models import Page
from databridge.confluence.parser import ADFParser
from databridge.ingest.markdown import SourceDocument


def page_to_source_document(
    page: Page,
    *,
    space_key: str,
    breadcrumb: str | None,
    parser: ADFParser | None = None,
) -> SourceDocument:
    """Normalize a full Confluence page without permitting destructive empty writes."""
    body_format = page.body.atlas_doc_format if page.body is not None else None
    raw = body_format.value if body_format is not None else None
    if raw is None or not raw.strip():
        raise EmptyBodyError("Confluence page has no ADF body", page_id=page.id)
    active_parser = parser or ADFParser()
    document = active_parser.parse_json(raw)
    markdown = active_parser.to_markdown(document)
    if not markdown.strip():
        raise EmptyBodyError("Confluence page rendered to an empty body", page_id=page.id)
    return SourceDocument(
        source_id=page.id,
        title=page.title,
        space_key=space_key,
        body=markdown,
        breadcrumb=breadcrumb,
    )
