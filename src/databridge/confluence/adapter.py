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


def page_has_only_children_extension(page: Page, *, parser: ADFParser | None = None) -> bool:
    """Return whether the ADF body is solely a non-content children macro."""
    body_format = page.body.atlas_doc_format if page.body is not None else None
    raw = body_format.value if body_format is not None else None
    if raw is None or not raw.strip():
        return False
    document = (parser or ADFParser()).parse_json(raw)
    meaningful_nodes = [
        node
        for node in document.content
        # Confluence commonly leaves an empty trailing paragraph after a macro.
        # Keep every other empty-looking node (for example, rule) conservatively.
        if not (node.type == "paragraph" and not node.text and not node.content)
    ]
    if len(meaningful_nodes) != 1:
        return False
    node = meaningful_nodes[0]
    return (
        node.type in {"extension", "bodiedExtension", "inlineExtension"}
        and str(node.attrs.get("extensionKey", "")).strip().casefold() == "children"
        and not node.content
        and not node.text
    )
