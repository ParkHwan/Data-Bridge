"""Strict versioned loader for the Data Bridge golden-set schema."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from databridge.evals.observation import CitationKind

GoldenKind = Literal["knowledge", "data", "report", "refusal"]
AgentName = Literal["knowledge_agent", "data_agent", "report_agent", "databridge_root"]

D9_FINAL_AGENT_TODO = "TODO(D-9): live 표본으로 확정"
_ID_RE = re.compile(r"^DG-\d{3}$")
_REPORT_HEADERS = ("Owner", "Action", "Due", "Source")


class GoldenSchemaError(ValueError):
    """Raised when a golden YAML file violates the v2 contract."""


class GoldenItem(BaseModel):
    """One fully validated v2 golden item."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: GoldenKind
    question: str
    expected_source_id: str | None = None
    expected_source_ids: tuple[str, ...] | None = None
    expected_keywords: tuple[tuple[str, ...], ...] | None = None
    min_keyword: float = Field(default=1.0, strict=True, gt=0.0, le=1.0)
    expected_citation_kind: CitationKind | None = None
    expected_final_agent: AgentName | Literal["TODO(D-9): live 표본으로 확정"] | None = None
    required_agents: tuple[AgentName, ...] = ()
    expected_tools_by_agent: dict[AgentName, tuple[str, ...]] | None = None
    strict_tools: bool = Field(default=True, strict=True)
    max_tool_calls: int | None = Field(default=None, strict=True, ge=0)
    max_dropped_claims: int | None = Field(default=None, strict=True, ge=0)
    expected_exact_value: str | int | float | None = None
    expected_table_headers: tuple[str, ...] | None = None
    min_table_rows: int | None = Field(default=None, strict=True, ge=1)

    @field_validator("id", "question", "expected_source_id")
    @classmethod
    def _trim_optional_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("string fields must be non-empty after trimming")
        return stripped

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("id must match ^DG-\\d{3}$")
        return value

    @field_validator("expected_source_ids")
    @classmethod
    def _validate_source_ids(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        cleaned = tuple(item.strip() for item in value)
        if not cleaned or any(not item for item in cleaned):
            raise ValueError("expected_source_ids must be a non-empty list of strings")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("expected_source_ids contains duplicates")
        return cleaned

    @field_validator("expected_keywords")
    @classmethod
    def _validate_keywords(
        cls, value: tuple[tuple[str, ...], ...] | None
    ) -> tuple[tuple[str, ...], ...] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("expected_keywords must contain at least one group")
        cleaned: list[tuple[str, ...]] = []
        for group in value:
            aliases = tuple(alias.strip() for alias in group)
            if not aliases or any(not alias for alias in aliases):
                raise ValueError("keyword groups and aliases must be non-empty")
            if len(set(aliases)) != len(aliases):
                raise ValueError("keyword group contains duplicates")
            cleaned.append(aliases)
        return tuple(cleaned)

    @field_validator("required_agents")
    @classmethod
    def _validate_required_agents(cls, value: tuple[AgentName, ...]) -> tuple[AgentName, ...]:
        if len(set(value)) != len(value):
            raise ValueError("required_agents contains duplicates")
        return value

    @field_validator("expected_tools_by_agent")
    @classmethod
    def _validate_tools(
        cls, value: dict[AgentName, tuple[str, ...]] | None
    ) -> dict[AgentName, tuple[str, ...]] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("expected_tools_by_agent must be non-empty")
        cleaned: dict[AgentName, tuple[str, ...]] = {}
        for agent, tools in value.items():
            names = tuple(tool.strip() for tool in tools)
            if not names or any(not tool for tool in names):
                raise ValueError("tool lists and names must be non-empty")
            if len(set(names)) != len(names):
                raise ValueError(f"tool list for {agent} contains duplicates")
            cleaned[agent] = names
        return cleaned

    @field_validator("expected_exact_value", mode="before")
    @classmethod
    def _validate_exact_value(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("expected_exact_value must not be boolean")
        if isinstance(value, str) and not value.strip():
            raise ValueError("expected_exact_value must be non-empty")
        return value

    @field_validator("expected_table_headers")
    @classmethod
    def _validate_headers(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        cleaned = tuple(header.strip() for header in value)
        if any(not header for header in cleaned):
            raise ValueError("table headers must be non-empty")
        return cleaned

    @model_validator(mode="after")
    def _validate_contract(self) -> GoldenItem:
        fields = self.model_fields_set
        source_xor_failed = (self.expected_source_id is None) == (
            self.expected_source_ids is None
        )
        source_required_or_present = self.kind in {"knowledge", "report"} or (
            self.expected_source_id is not None or self.expected_source_ids is not None
        )
        if source_xor_failed and source_required_or_present:
            raise ValueError("set exactly one of expected_source_id/expected_source_ids")

        if self.kind == "refusal":
            forbidden = {
                "expected_source_id",
                "expected_source_ids",
                "expected_keywords",
                "min_keyword",
                "expected_citation_kind",
                "expected_final_agent",
                "required_agents",
                "expected_tools_by_agent",
                "strict_tools",
                "max_tool_calls",
                "max_dropped_claims",
                "expected_exact_value",
                "expected_table_headers",
                "min_table_rows",
            }
            present = sorted(fields & forbidden)
            if present:
                raise ValueError(f"refusal item forbids fields: {', '.join(present)}")
            return self

        if "min_keyword" in fields and self.expected_keywords is None:
            raise ValueError("min_keyword requires expected_keywords")
        if "strict_tools" in fields and self.expected_tools_by_agent is None:
            raise ValueError("strict_tools requires expected_tools_by_agent")
        if self.min_table_rows is not None and self.expected_table_headers is None:
            raise ValueError("min_table_rows requires expected_table_headers")
        if self.kind != "report" and (
            self.expected_table_headers is not None or self.min_table_rows is not None
        ):
            raise ValueError("table assertion fields are only allowed for report items")
        if self.max_tool_calls is not None:
            if self.expected_tools_by_agent is None:
                raise ValueError("max_tool_calls requires expected_tools_by_agent")
            if self.max_tool_calls == 0:
                raise ValueError("max_tool_calls must be positive when tools are required")

        if self.expected_final_agent is None:
            raise ValueError("non-refusal item requires expected_final_agent")
        if self.expected_citation_kind is None:
            raise ValueError("non-refusal item requires expected_citation_kind")

        required_agent = f"{self.kind}_agent"
        if required_agent not in self.required_agents:
            raise ValueError(f"{self.kind} item requires {required_agent} in required_agents")
        if (
            self.expected_tools_by_agent is None
            or required_agent not in self.expected_tools_by_agent
        ):
            raise ValueError(f"{self.kind} item requires tools for {required_agent}")

        if self.kind == "knowledge":
            self._require_tool("knowledge_agent", ("search_knowledge",))
            if self.expected_citation_kind != "document":
                raise ValueError("knowledge item requires document citation kind")
            if self.expected_source_id is None and self.expected_source_ids is None:
                raise ValueError("knowledge item requires source expectation")
            if self.expected_keywords is None:
                raise ValueError("knowledge item requires expected_keywords")
        elif self.kind == "report":
            self._require_tool("report_agent", ("search_knowledge",))
            if self.expected_citation_kind != "document":
                raise ValueError("report item requires document citation kind")
            if self.expected_source_id is None and self.expected_source_ids is None:
                raise ValueError("report item requires source expectation")
            if self.expected_table_headers != _REPORT_HEADERS:
                raise ValueError("report item requires exact Owner|Action|Due|Source headers")
            if self.min_table_rows is None:
                raise ValueError("report item requires min_table_rows")
        elif self.kind == "data":
            self._require_tool("data_agent", ("list_tables", "query_bigquery"))
            if self.expected_citation_kind != "bigquery":
                raise ValueError("data item requires bigquery citation kind")
            if self.expected_exact_value is None and self.expected_keywords is None:
                raise ValueError("data item requires exact value or keywords")
        return self

    def _require_tool(self, agent: AgentName, expected: tuple[str, ...]) -> None:
        if self.expected_tools_by_agent is None:
            raise ValueError("expected_tools_by_agent is required")
        if self.expected_tools_by_agent.get(agent) != expected:
            raise ValueError(f"{self.kind} item requires {agent} tools {list(expected)}")

    @property
    def expected_sources(self) -> frozenset[str]:
        if self.expected_source_id is not None:
            return frozenset((self.expected_source_id,))
        return frozenset(self.expected_source_ids or ())

    @property
    def final_agent_is_pending(self) -> bool:
        return self.expected_final_agent == D9_FINAL_AGENT_TODO


class GoldenSet(BaseModel):
    """Top-level v2 golden document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    items: tuple[GoldenItem, ...]

    @field_validator("version", mode="before")
    @classmethod
    def _validate_version(cls, value: object) -> object:
        if isinstance(value, bool) or value != 2:
            raise ValueError("version must be the integer 2")
        return value

    @field_validator("items")
    @classmethod
    def _validate_items(cls, value: tuple[GoldenItem, ...]) -> tuple[GoldenItem, ...]:
        if not value:
            raise ValueError("items must be non-empty")
        ids = [item.id for item in value]
        if len(set(ids)) != len(ids):
            raise ValueError("golden item ids must be unique")
        return value


def load_golden(path: Path) -> GoldenSet:
    """Load and strictly validate a UTF-8 YAML v2 golden file."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise GoldenSchemaError("golden document must be a mapping")
        return GoldenSet.model_validate(cast(dict[str, Any], raw))
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise GoldenSchemaError(f"invalid golden file {path}: {exc}") from exc
