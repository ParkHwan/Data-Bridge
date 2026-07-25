"""Resolve Confluence ancestor paths with bounded, cached API traversal."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from databridge.confluence.exceptions import ConfluenceAPIError
from databridge.confluence.models import Page

logger = logging.getLogger(__name__)
_PERMANENT_CUT_STATUSES = {403, 404}


class AncestorClient(Protocol):
    async def get_page(self, page_id: str, *, body_format: str = "atlas_doc_format") -> Page: ...

    async def get_folder(self, folder_id: str) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class AncestorNode:
    title: str
    parent_id: str | None
    parent_type: str | None


class AncestorResolver:
    def __init__(self, client: AncestorClient, *, max_depth: int = 20) -> None:
        if max_depth < 1:
            raise ValueError("max_depth must be positive")
        self._client = client
        self._max_depth = max_depth
        self._cache: dict[str, AncestorNode] = {}

    def seed_page(self, page: Page) -> None:
        self._cache[page.id] = AncestorNode(page.title, page.parent_id, page.parent_type)

    async def resolve(self, parent_id: str | None, parent_type: str | None) -> list[str]:
        current_id = parent_id
        current_type = parent_type or "page"
        visited: set[str] = set()
        leaf_first: list[str] = []
        while current_id:
            if current_id in visited:
                logger.warning("ancestor cycle: node_id=%s", current_id)
                break
            if len(visited) >= self._max_depth:
                logger.warning(
                    "ancestor depth exceeded: node_id=%s max_depth=%s",
                    current_id,
                    self._max_depth,
                )
                break
            visited.add(current_id)
            try:
                node = await self._get_node(current_id, current_type)
            except ConfluenceAPIError as exc:
                if exc.status_code not in _PERMANENT_CUT_STATUSES:
                    raise
                logger.warning(
                    "ancestor chain cut: node_id=%s node_type=%s status_code=%s",
                    current_id,
                    current_type,
                    exc.status_code,
                )
                break
            if node is None:
                break
            leaf_first.append(node.title)
            current_id, current_type = node.parent_id, node.parent_type or "page"
        return list(reversed(leaf_first))

    async def _get_node(self, node_id: str, node_type: str) -> AncestorNode | None:
        if node_id in self._cache:
            return self._cache[node_id]
        normalized = node_type.lower()
        if normalized == "page":
            page = await self._client.get_page(node_id)
            node = AncestorNode(page.title, page.parent_id, page.parent_type)
        elif normalized == "folder":
            folder = await self._client.get_folder(node_id)
            title = str(folder.get("title") or folder.get("name") or node_id)
            parent_id = folder.get("parentId") or folder.get("parent_id")
            parent_type = folder.get("parentType") or folder.get("parent_type")
            node = AncestorNode(
                title,
                str(parent_id) if parent_id else None,
                str(parent_type) if parent_type else None,
            )
        else:
            logger.warning("unsupported ancestor type: node_id=%s node_type=%s", node_id, node_type)
            return None
        self._cache[node_id] = node
        return node
