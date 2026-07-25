"""Minimal Pydantic contracts for Confluence REST v2 and ADF payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConfluenceModel(BaseModel):
    """Common permissive API model; Confluence adds response fields over time."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Space(ConfluenceModel):
    id: str
    key: str
    name: str | None = None
    type: str | None = None


class Version(ConfluenceModel):
    number: int
    created_at: datetime | None = Field(default=None, alias="createdAt")


class BodyFormat(ConfluenceModel):
    value: str | None = None
    representation: str | None = None


class Body(ConfluenceModel):
    atlas_doc_format: BodyFormat | None = None


class Page(ConfluenceModel):
    id: str
    title: str
    type: str | None = None
    status: str = "current"
    space_id: str | None = Field(default=None, alias="spaceId")
    parent_id: str | None = Field(default=None, alias="parentId")
    parent_type: str | None = Field(default=None, alias="parentType")
    version: Version | None = None
    body: Body | None = None


class PaginationLinks(ConfluenceModel):
    next: str | None = None


class PageListResponse(ConfluenceModel):
    results: list[Page] = Field(default_factory=list)
    links: PaginationLinks | None = Field(default=None, alias="_links")


class SpaceListResponse(ConfluenceModel):
    results: list[Space] = Field(default_factory=list)
    links: PaginationLinks | None = Field(default=None, alias="_links")


class ADFMark(ConfluenceModel):
    type: str
    attrs: dict[str, Any] = Field(default_factory=dict)


class ADFNode(ConfluenceModel):
    type: str
    text: str | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)
    marks: list[ADFMark] = Field(default_factory=list)
    content: list[ADFNode] = Field(default_factory=list)


class ADFDocument(ConfluenceModel):
    type: str
    version: int = 1
    content: list[ADFNode] = Field(default_factory=list)
