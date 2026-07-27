"""Offline contract tests for the Confluence integration."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from databridge.confluence.adapter import (
    page_has_only_children_extension,
    page_to_source_document,
)
from databridge.confluence.ancestors import AncestorResolver
from databridge.confluence.batch import ConfluenceBatchConfig, run_confluence_batch
from databridge.confluence.client import ConfluenceClient
from databridge.confluence.exceptions import (
    BatchSafetyError,
    ConfluenceAPIError,
    ConfluenceAuthError,
    ConfluenceRateLimitError,
    EmptyBodyError,
)
from databridge.confluence.models import Body, BodyFormat, Page, Space
from databridge.confluence.parser import ADFParser
from databridge.embed import HashedEmbedder
from databridge.ingest.chunker import Chunk, chunk_document

FIXTURES = Path(__file__).parent / "fixtures"


def _text(value: str, *, marks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": value}
    if marks:
        node["marks"] = marks
    return node


def _paragraph(value: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [_text(value)]}


def _adf(*nodes: dict[str, Any]) -> str:
    return json.dumps({"type": "doc", "version": 1, "content": list(nodes)})


def _page(
    page_id: str,
    *,
    title: str | None = None,
    space_id: str = "space-1",
    parent_id: str | None = "folder-1",
    parent_type: str | None = "folder",
    body: str | None = None,
) -> Page:
    return Page(
        id=page_id,
        title=title or page_id,
        type="page",
        spaceId=space_id,
        parentId=parent_id,
        parentType=parent_type,
        body=Body(atlas_doc_format=BodyFormat(value=body or _adf(_paragraph(page_id)))),
    )


@pytest.mark.asyncio
@respx.mock
async def test_client_uses_basic_auth_and_paginates_folder_descendants() -> None:
    route = respx.get("https://example.atlassian.net/wiki/api/v2/folders/f-1/descendants").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "results": [{"id": "1", "title": "One", "type": "page"}],
                    "_links": {"next": "/wiki/api/v2/folders/f-1/descendants?cursor=next"},
                },
            ),
            httpx.Response(200, json={"results": [{"id": "2", "title": "Two", "type": "page"}]}),
        ]
    )
    async with ConfluenceClient(
        base_url="https://example.atlassian.net",
        email="owner@example.com",
        api_token="secret",
    ) as client:
        pages = [
            page async for page in client.iter_folder_descendants("f-1", batch_size=2, depth=7)
        ]
    assert [page.id for page in pages] == ["1", "2"]
    assert route.calls[0].request.headers["Authorization"].startswith("Basic ")
    assert route.calls[1].request.url.params["cursor"] == "next"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, ConfluenceAuthError), (403, ConfluenceAPIError), (429, ConfluenceRateLimitError)],
)
async def test_client_maps_http_errors(status: int, error_type: type[Exception]) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(status))
    async with ConfluenceClient(
        base_url="https://example.atlassian.net",
        api_token="token",
        max_attempts=1,
        transport=transport,
    ) as client:
        with pytest.raises(error_type):
            await client.get_folder("f-1")


@pytest.mark.asyncio
async def test_client_retries_rate_limit_and_uses_bearer_without_email() -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"id": "f-1"})

    async with ConfluenceClient(
        base_url="https://example.atlassian.net",
        api_token="token",
        max_attempts=2,
        transport=httpx.MockTransport(respond),
    ) as client:
        assert await client.get_folder("f-1") == {"id": "f-1"}
    assert len(calls) == 2
    assert calls[0].headers["Authorization"] == "Bearer token"


@pytest.mark.asyncio
async def test_client_rejects_multi_cursor_cycle() -> None:
    cursors = iter(("A", "B", "A"))
    calls: list[str | None] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("cursor"))
        return httpx.Response(
            200,
            json={"results": [], "_links": {"next": f"/descendants?cursor={next(cursors)}"}},
        )

    async with ConfluenceClient(
        base_url="https://example.atlassian.net",
        api_token="token",
        max_attempts=1,
        transport=httpx.MockTransport(respond),
    ) as client:
        with pytest.raises(ConfluenceAPIError, match="invalid cursor"):
            _ = [page async for page in client.iter_folder_descendants("f-1")]
    assert calls == [None, "A", "B"]


def test_parser_renders_structural_and_inline_adf_nodes() -> None:
    fixture = _adf(
        {"type": "heading", "attrs": {"level": 2}, "content": [_text("Overview")]},
        {
            "type": "paragraph",
            "content": [
                _text("Bold", marks=[{"type": "strong"}]),
                _text(" and "),
                _text("link", marks=[{"type": "link", "attrs": {"href": "https://x"}}]),
                {"type": "hardBreak"},
                {"type": "emoji", "attrs": {"text": "🎉"}},
                {"type": "status", "attrs": {"text": "READY"}},
                {"type": "mention", "attrs": {"text": "@Alex"}},
                {"type": "date", "attrs": {"timestamp": "0"}},
            ],
        },
        {
            "type": "bulletList",
            "content": [
                {"type": "listItem", "content": [_paragraph("Bullet")]},
                {
                    "type": "listItem",
                    "content": [
                        _paragraph("Nested"),
                        {
                            "type": "orderedList",
                            "attrs": {"order": 3},
                            "content": [{"type": "listItem", "content": [_paragraph("Third")]}],
                        },
                    ],
                },
            ],
        },
        {"type": "codeBlock", "attrs": {"language": "python"}, "content": [_text("x = 1")]},
        {"type": "blockquote", "content": [_paragraph("Quoted")]},
        {"type": "rule"},
        {
            "type": "panel",
            "attrs": {"panelType": "info"},
            "content": [_paragraph("Panel body")],
        },
        {"type": "expand", "attrs": {"title": "Details"}, "content": [_paragraph("More")]},
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        {"type": "tableHeader", "content": [_paragraph("Name")]},
                        {"type": "tableHeader", "content": [_paragraph("Owner")]},
                    ],
                },
                {
                    "type": "tableRow",
                    "content": [
                        {"type": "tableCell", "content": [_paragraph("Bridge")]},
                        {"type": "tableCell", "content": [_paragraph("")]},
                    ],
                },
            ],
        },
        {"type": "mediaGroup", "content": [{"type": "media", "attrs": {"id": "m-1"}}]},
        {"type": "inlineCard", "attrs": {"url": "https://card"}},
        {
            "type": "taskList",
            "content": [
                {"type": "taskItem", "attrs": {"state": "DONE"}, "content": [_text("Ship")]}
            ],
        },
        {
            "type": "decisionList",
            "content": [{"type": "decisionItem", "content": [_text("Use REST")]}],
        },
        {
            "type": "bodiedExtension",
            "attrs": {"extensionKey": "toc"},
            "content": [_paragraph("Body")],
        },
        {"type": "unknownContainer", "content": [_text("Preserved unknown text")]},
    )
    parser = ADFParser()
    markdown = parser.to_markdown(parser.parse_json(fixture))

    assert (
        markdown
        == """## Overview

**Bold** and [link](https://x)
🎉`READY`@Alex1970-01-01

- Bullet
- Nested
  3. Third

```python
x = 1
```

> Quoted

---

**Info:**

> Panel body

**Details:**

More

| Name | Owner |
| --- | --- |
| Bridge |  |

[media (attachment m-1)]

[https://card](https://card)

- [x] Ship

- Decision: Use REST

**toc**

Body

Preserved unknown text"""
    )


def test_parser_preserves_fenced_code_bytes_during_optimization() -> None:
    code = "line1\n\n\n<div>must stay</div>\n```\n<span>also code</span>"
    parser = ADFParser()
    document = parser.parse_json(
        _adf({"type": "codeBlock", "attrs": {"language": "text"}, "content": [_text(code)]})
    )

    assert parser.to_markdown(document) == (
        "````text\nline1\n\n\n<div>must stay</div>\n```\n<span>also code</span>\n````"
    )


def test_parser_uses_safe_inline_code_delimiter() -> None:
    parser = ADFParser()
    document = parser.parse_json(
        _adf(
            {
                "type": "paragraph",
                "content": [_text("a``b", marks=[{"type": "code"}])],
            }
        )
    )
    assert parser.to_markdown(document) == "```a``b```"


def test_parser_preserves_unknown_leaf_text() -> None:
    parser = ADFParser()
    document = parser.parse_json(_adf({"type": "futureLeaf", "text": "must survive"}))
    assert parser.to_markdown(document) == "must survive"


def test_parser_separates_multiple_media_placeholders() -> None:
    parser = ADFParser()
    document = parser.parse_json(
        _adf(
            {
                "type": "mediaGroup",
                "content": [
                    {"type": "media", "attrs": {"id": "one"}},
                    {"type": "media", "attrs": {"id": "two"}},
                ],
            }
        )
    )
    assert parser.to_markdown(document) == ("[media (attachment one)]\n[media (attachment two)]")


def test_parser_promotes_first_row_when_table_has_no_headers() -> None:
    parser = ADFParser()
    document = parser.parse_json(
        _adf(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [_paragraph("Name")]},
                            {"type": "tableCell", "content": [_paragraph("Owner")]},
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [_paragraph("Bridge")]},
                            {"type": "tableCell", "content": [_paragraph("Alex")]},
                        ],
                    },
                ],
            }
        )
    )
    assert parser.to_markdown(document) == ("| Name | Owner |\n| --- | --- |\n| Bridge | Alex |")


def test_parser_applies_row_and_cell_table_alignment() -> None:
    parser = ADFParser()
    document = parser.parse_json(
        _adf(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "attrs": {"align": "center"},
                        "content": [
                            {"type": "tableHeader", "content": [_paragraph("Name")]},
                            {
                                "type": "tableHeader",
                                "attrs": {"align": "right"},
                                "content": [_paragraph("Count")],
                            },
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [_paragraph("Bridge")]},
                            {"type": "tableCell", "content": [_paragraph("2")]},
                        ],
                    },
                ],
            }
        )
    )
    assert parser.to_markdown(document) == ("| Name | Count |\n| :---: | ---: |\n| Bridge | 2 |")


def test_parser_flattens_table_spans_while_preserving_values() -> None:
    parser = ADFParser()
    document = parser.parse_json(
        _adf(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableHeader",
                                "attrs": {"colspan": 2},
                                "content": [_paragraph("Group")],
                            },
                            {
                                "type": "tableHeader",
                                "attrs": {"rowspan": 2},
                                "content": [_paragraph("Owner")],
                            },
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [_paragraph("A")]},
                            {"type": "tableCell", "content": [_paragraph("B")]},
                        ],
                    },
                ],
            }
        )
    )
    assert parser.to_markdown(document) == (
        "| Group | Group | Owner |\n| --- | --- | --- |\n| A | B | Owner |"
    )


def test_adapter_rejects_missing_and_rendered_empty_bodies() -> None:
    missing = Page(id="1", title="Missing", type="page", body=None)
    empty = _page("2", body=_adf({"type": "paragraph", "content": []}))
    with pytest.raises(EmptyBodyError):
        page_to_source_document(missing, space_key="CONF_DEMO", breadcrumb=None)
    with pytest.raises(EmptyBodyError):
        page_to_source_document(empty, space_key="CONF_DEMO", breadcrumb=None)


def test_children_extension_classifier_requires_the_macro_to_be_the_only_content() -> None:
    children = _page(
        "container",
        body=_adf({"type": "extension", "attrs": {"extensionKey": "children"}}),
    )
    children_with_empty_paragraph = _page(
        "container-with-trailing-paragraph",
        body=_adf(
            {"type": "extension", "attrs": {"extensionKey": "children"}},
            {"type": "paragraph"},
        ),
    )
    with_text = _page(
        "content",
        body=_adf(
            {"type": "extension", "attrs": {"extensionKey": "children"}},
            _paragraph("Meaningful body"),
        ),
    )
    with_heading = _page(
        "content-with-heading",
        body=_adf(
            {"type": "heading", "attrs": {"level": 2}, "content": [_text("Heading")]},
            {"type": "extension", "attrs": {"extensionKey": "children"}},
        ),
    )
    literal = _page("literal", body=_adf(_paragraph("[children]")))

    assert page_has_only_children_extension(children)
    assert page_has_only_children_extension(children_with_empty_paragraph)
    assert not page_has_only_children_extension(with_text)
    assert not page_has_only_children_extension(with_heading)
    assert not page_has_only_children_extension(literal)


def test_synthetic_adf_contract_preserves_quality_critical_structure() -> None:
    body = (FIXTURES / "confluence_quality.adf.json").read_text(encoding="utf-8")
    page = _page(
        "quality-fixture",
        title="Quality Fixture",
        body=body,
    )
    document = page_to_source_document(
        page,
        space_key="FIXTURE",
        breadcrumb="Lab > Data Processing",
    )

    markdown = document.body
    assert "## Vision" in markdown
    assert "### Pipeline" in markdown
    assert "#### Expanded Matrix" in markdown
    assert "```python\nprint('quality')\n```" in markdown
    table_lines = [line for line in markdown.splitlines() if line.startswith("|")]
    assert len(table_lines) == 6
    assert not any(line.startswith("> |") for line in markdown.splitlines())
    assert "[Architecture (attachment media-1)]" in markdown
    assert "[https://example.test/card](https://example.test/card)" in markdown
    assert "[children]" in markdown

    chunks = chunk_document(document)
    assert [chunk.heading for chunk in chunks] == [
        "Vision",
        "Vision > Pipeline",
        "Vision > Pipeline > Expanded Matrix",
    ]
    assert all(chunk.heading for chunk in chunks)
    assert all(chunk.source_id == "quality-fixture" for chunk in chunks)
    assert all(chunk.space_key == "FIXTURE" for chunk in chunks)
    assert all(chunk.breadcrumb == "Lab > Data Processing" for chunk in chunks)
    assert not page_has_only_children_extension(page)


@pytest.mark.parametrize("kind", ["expand", "nestedExpand"])
def test_expand_variants_flatten_body_without_changing_panel_contract(kind: str) -> None:
    fixture = _adf(
        {
            "type": kind,
            "attrs": {"title": "Details"},
            "content": [
                {"type": "heading", "attrs": {"level": 2}, "content": [_text("Anchor")]},
                _paragraph("Body"),
            ],
        }
    )
    markdown = ADFParser().to_markdown(ADFParser().parse_json(fixture))
    assert markdown == "**Details:**\n\n## Anchor\n\nBody"
    assert ">" not in markdown


class _AncestorClient:
    def __init__(
        self,
        pages: dict[str, Page | Exception],
        folders: dict[str, dict[str, object]],
    ) -> None:
        self.pages = pages
        self.folders = folders
        self.page_calls: list[str] = []

    async def get_page(self, page_id: str, *, body_format: str = "atlas_doc_format") -> Page:
        del body_format
        self.page_calls.append(page_id)
        value = self.pages[page_id]
        if isinstance(value, Exception):
            raise value
        return value

    async def get_folder(self, folder_id: str) -> dict[str, object]:
        return self.folders[folder_id]


@pytest.mark.asyncio
async def test_ancestor_resolver_orders_caches_and_cuts_cycles() -> None:
    client = _AncestorClient(
        {
            "child": _page("child", parent_id="root", parent_type="page"),
            "root": _page("root", parent_id="child", parent_type="page"),
        },
        {},
    )
    resolver = AncestorResolver(client)
    assert await resolver.resolve("child", "page") == ["root", "child"]
    assert await resolver.resolve("child", "page") == ["root", "child"]
    assert client.page_calls == ["child", "root"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_ancestor_resolver_cuts_permanent_api_errors(status: int) -> None:
    error = ConfluenceAPIError("cut", status_code=status)
    resolver = AncestorResolver(_AncestorClient({"missing": error}, {}))
    assert await resolver.resolve("missing", "page") == []


@pytest.mark.asyncio
async def test_ancestor_resolver_stops_at_depth_limit() -> None:
    client = _AncestorClient(
        {
            "one": _page("one", parent_id="two", parent_type="page"),
            "two": _page("two", parent_id=None),
        },
        {},
    )
    resolver = AncestorResolver(client, max_depth=1)
    assert await resolver.resolve("one", "page") == ["one"]
    assert client.page_calls == ["one"]


class _BatchClient:
    def __init__(self, pages: list[Page], *, fail_page: str | None = None) -> None:
        self.pages = pages
        self.fail_page = fail_page

    async def get_space_by_key(self, space_key: str) -> Space:
        return Space(id="space-1", key=space_key)

    async def get_folder(self, folder_id: str) -> dict[str, object]:
        return {"id": folder_id, "spaceId": "space-1", "title": "Folder"}

    async def iter_folder_descendants(
        self, folder_id: str, *, batch_size: int = 50, depth: int = 10
    ) -> AsyncIterator[Page]:
        del folder_id, batch_size, depth
        for page in self.pages:
            yield page

    async def get_page(self, page_id: str, *, body_format: str = "atlas_doc_format") -> Page:
        del body_format
        if page_id == self.fail_page:
            raise ConfluenceAPIError("failed", status_code=500)
        return next(page for page in self.pages if page.id == page_id)


class _BatchStore:
    def __init__(
        self,
        existing: set[str] | None = None,
        *,
        acquired: bool = True,
        fail_replace: str | None = None,
    ) -> None:
        self.existing = existing or set()
        self.acquired = acquired
        self.fail_replace = fail_replace
        self.events: list[str] = []

    @contextmanager
    def advisory_lock(self, key: str) -> Iterator[bool]:
        self.events.append(f"lock:{key}")
        try:
            yield self.acquired
        finally:
            self.events.append("unlock")

    def replace_source(
        self,
        *,
        space_key: str,
        source_id: str,
        chunks: list[Chunk],
        embeddings: list[list[float]],
    ) -> int:
        assert space_key == "CONF_DEMO"
        assert chunks and len(chunks) == len(embeddings)
        self.events.append(f"replace:{source_id}")
        if source_id == self.fail_replace:
            raise RuntimeError("database write failed")
        return len(chunks)

    def list_source_ids(self, *, space_key: str) -> set[str]:
        assert space_key == "CONF_DEMO"
        self.events.append("list")
        return set(self.existing)

    def delete_source(self, *, space_key: str, source_id: str) -> int:
        assert space_key == "CONF_DEMO"
        self.events.append(f"delete:{source_id}")
        return 1


@pytest.mark.asyncio
async def test_batch_replaces_all_pages_before_safe_gc() -> None:
    pages = [_page("p1"), _page("p2")]
    store = _BatchStore({"p1", "stale"})
    result = await run_confluence_batch(
        client=_BatchClient(pages),
        store=store,
        embedder=HashedEmbedder(),
        config=ConfluenceBatchConfig(space_key="CONF_DEMO", folder_id="folder-1"),
    )
    assert result.pages == 2
    assert result.vertex_calls == 2
    assert store.events[1:] == ["replace:p1", "replace:p2", "list", "delete:stale", "unlock"]


@pytest.mark.asyncio
async def test_batch_skips_empty_page_and_ingests_remaining_pages() -> None:
    empty = _page("empty", body=_adf({"type": "paragraph"}))
    store = _BatchStore()
    result = await run_confluence_batch(
        client=_BatchClient([_page("p1"), empty]),
        store=store,
        embedder=HashedEmbedder(),
        config=ConfluenceBatchConfig(space_key="CONF_DEMO", folder_id="folder-1"),
    )
    assert result.pages == 1
    assert result.skipped_pages == 1
    assert store.events[1:] == ["replace:p1", "list", "unlock"]


@pytest.mark.asyncio
async def test_batch_preserves_existing_source_for_skipped_empty_page() -> None:
    empty = _page("empty", body=_adf({"type": "paragraph"}))
    store = _BatchStore({"empty", "stale"})
    await run_confluence_batch(
        client=_BatchClient([_page("p1"), empty]),
        store=store,
        embedder=HashedEmbedder(),
        config=ConfluenceBatchConfig(space_key="CONF_DEMO", folder_id="folder-1"),
    )
    assert "delete:empty" not in store.events
    assert "delete:stale" in store.events


@pytest.mark.asyncio
async def test_batch_suppresses_children_only_page_and_garbage_collects_old_chunk() -> None:
    container = _page(
        "container",
        body=_adf({"type": "extension", "attrs": {"extensionKey": "children"}}),
    )
    store = _BatchStore({"container"})
    result = await run_confluence_batch(
        client=_BatchClient([_page("p1"), container]),
        store=store,
        embedder=HashedEmbedder(),
        config=ConfluenceBatchConfig(space_key="CONF_DEMO", folder_id="folder-1"),
    )

    assert result.pages == 1
    assert result.skipped_pages == 0
    assert result.suppressed_pages == 1
    assert store.events[1:] == ["replace:p1", "list", "delete:container", "unlock"]


@pytest.mark.asyncio
async def test_batch_rejects_all_suppressed_pages_without_store_mutation() -> None:
    container = _page(
        "container",
        body=_adf({"type": "extension", "attrs": {"extensionKey": "children"}}),
    )
    store = _BatchStore({"container"})
    with pytest.raises(BatchSafetyError, match="All pages produced no content"):
        await run_confluence_batch(
            client=_BatchClient([container]),
            store=store,
            embedder=HashedEmbedder(),
            config=ConfluenceBatchConfig(space_key="CONF_DEMO", folder_id="folder-1"),
        )
    assert store.events == [
        "lock:databridge:confluence:CONF_DEMO:folder-1",
        "unlock",
    ]


@pytest.mark.asyncio
async def test_batch_rejects_all_empty_pages_without_store_mutation() -> None:
    pages = [
        _page("empty-1", body=_adf({"type": "paragraph"})),
        _page("empty-2", body=_adf({"type": "paragraph"})),
    ]
    store = _BatchStore({"empty-1", "stale"})
    with pytest.raises(BatchSafetyError, match="All pages produced no content"):
        await run_confluence_batch(
            client=_BatchClient(pages),
            store=store,
            embedder=HashedEmbedder(),
            config=ConfluenceBatchConfig(space_key="CONF_DEMO", folder_id="folder-1"),
        )
    assert store.events == [
        "lock:databridge:confluence:CONF_DEMO:folder-1",
        "unlock",
    ]


@pytest.mark.asyncio
async def test_batch_failure_never_mutates_or_garbage_collects() -> None:
    store = _BatchStore({"stale"})
    with pytest.raises(ConfluenceAPIError):
        await run_confluence_batch(
            client=_BatchClient([_page("p1"), _page("p2")], fail_page="p2"),
            store=store,
            embedder=HashedEmbedder(),
            config=ConfluenceBatchConfig(space_key="CONF_DEMO", folder_id="folder-1"),
        )
    assert store.events == [
        "lock:databridge:confluence:CONF_DEMO:folder-1",
        "unlock",
    ]


@pytest.mark.asyncio
async def test_batch_store_failure_does_not_enter_gc_phase() -> None:
    store = _BatchStore({"stale"}, fail_replace="p2")
    with pytest.raises(RuntimeError, match="database write failed"):
        await run_confluence_batch(
            client=_BatchClient([_page("p1"), _page("p2")]),
            store=store,
            embedder=HashedEmbedder(),
            config=ConfluenceBatchConfig(space_key="CONF_DEMO", folder_id="folder-1"),
        )
    assert store.events == [
        "lock:databridge:confluence:CONF_DEMO:folder-1",
        "replace:p1",
        "replace:p2",
        "unlock",
    ]


@pytest.mark.asyncio
async def test_batch_rejects_empty_traversal_without_gc() -> None:
    store = _BatchStore({"stale"})
    with pytest.raises(BatchSafetyError, match="no pages"):
        await run_confluence_batch(
            client=_BatchClient([]),
            store=store,
            embedder=HashedEmbedder(),
            config=ConfluenceBatchConfig(space_key="CONF_DEMO", folder_id="folder-1"),
        )
    assert "list" not in store.events
