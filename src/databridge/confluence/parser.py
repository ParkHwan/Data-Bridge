"""Atlassian Document Format to Markdown conversion.

The renderer deliberately accepts unknown ADF nodes. Unknown containers retain their
children instead of silently dropping text, while known structural nodes preserve the
Markdown boundaries needed by the existing section-aware chunker.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from pydantic import ValidationError

from databridge.confluence.exceptions import ADFParseError
from databridge.confluence.models import ADFDocument, ADFMark, ADFNode

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _RenderContext:
    list_depth: int = 0
    ordered_index: int | None = None
    table_header: bool = False
    inline: bool = False


class ADFParser:
    """Parse JSON ADF and render its supported semantics as Markdown."""

    def __init__(self, *, max_depth: int = 50) -> None:
        if max_depth < 1:
            raise ValueError("max_depth must be positive")
        self._max_depth = max_depth

    def parse_json(self, adf_json: str) -> ADFDocument:
        try:
            raw = json.loads(adf_json)
            document = ADFDocument.model_validate(raw)
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise ADFParseError("Invalid ADF document") from exc
        if document.type != "doc":
            raise ADFParseError(f"ADF root type must be 'doc', got {document.type!r}")
        return document

    def to_text(self, document: ADFDocument) -> str:
        return "\n".join(part for part in self._plain_nodes(document.content, 0) if part).strip()

    def to_markdown(self, document: ADFDocument, *, enable_llm_optimization: bool = True) -> str:
        rendered = self._render_nodes(document.content, _RenderContext(), 0)
        markdown = self._normalize_blocks(rendered)
        return self._optimize_markdown_for_llm(markdown) if enable_llm_optimization else markdown

    def _render_nodes(self, nodes: list[ADFNode], context: _RenderContext, depth: int) -> str:
        if depth > self._max_depth:
            raise ADFParseError("ADF maximum nesting depth exceeded")
        return "".join(self._render_node(node, context, depth) for node in nodes)

    def _render_node(self, node: ADFNode, context: _RenderContext, depth: int) -> str:
        kind = node.type

        def children(ctx: _RenderContext = context) -> str:
            return self._render_nodes(node.content, ctx, depth + 1)

        if kind == "text":
            return self._apply_marks(node.text or "", node.marks)
        if kind == "hardBreak":
            return "  \n"
        if kind in {"paragraph", "caption"}:
            body = children(
                _RenderContext(
                    list_depth=context.list_depth,
                    ordered_index=context.ordered_index,
                    table_header=context.table_header,
                    inline=True,
                )
            ).strip()
            return f"{body}\n\n" if body else "\n"
        if kind == "heading":
            level = self._bounded_int(node.attrs.get("level"), default=1, low=1, high=6)
            body = children(_RenderContext(inline=True)).strip()
            return f"{'#' * level} {body}\n\n" if body else ""
        if kind == "blockquote":
            body = self._normalize_blocks(children()).strip()
            return "\n".join(f"> {line}" if line else ">" for line in body.splitlines()) + "\n\n"
        if kind == "rule":
            return "---\n\n"
        if kind in {"bulletList", "orderedList"}:
            return self._render_list(node, context, depth)
        if kind == "listItem":
            return self._render_list_item(node, context, depth)
        if kind == "codeBlock":
            language = self._safe_token(node.attrs.get("language"))
            code = self._plain_join(node.content, depth + 1)
            fence = self._code_fence(code)
            closing_separator = "" if code.endswith("\n") else "\n"
            return f"{fence}{language}\n{code}{closing_separator}{fence}\n\n"
        if kind == "inlineCard":
            url = self._string(node.attrs.get("url"))
            rendered = f"[{url}]({url})" if url else children()
            return rendered if context.inline else f"{rendered}\n\n"
        if kind in {"blockCard", "embedCard"}:
            url = self._string(node.attrs.get("url"))
            return f"[{url}]({url})\n\n" if url else children()
        if kind == "table":
            return self._render_table(node, depth)
        if kind in {"tableRow", "tableCell", "tableHeader"}:
            return children()
        if kind in {"panel", "expand", "nestedExpand"}:
            return self._render_container(node, context, depth)
        if kind in {"media", "mediaInline", "mediaSingle", "mediaGroup"}:
            return self._render_media(node, context, depth)
        if kind == "emoji":
            return self._string(node.attrs.get("text")) or self._string(node.attrs.get("shortName"))
        if kind == "status":
            text = self._string(node.attrs.get("text"))
            return f"`{text}`" if text else ""
        if kind == "mention":
            text = self._string(node.attrs.get("text"))
            identifier = self._string(node.attrs.get("id"))
            return text or (f"@{identifier}" if identifier else "@unknown")
        if kind == "date":
            return self._render_date(node.attrs.get("timestamp"))
        if kind in {"taskList", "decisionList"}:
            body = children(_RenderContext(list_depth=context.list_depth + 1)).strip()
            return f"{body}\n\n" if body else ""
        if kind in {"taskItem", "decisionItem"}:
            return self._render_action_item(node, context, depth)
        if kind in {"extension", "bodiedExtension", "inlineExtension"}:
            return self._render_extension(node, context, depth)

        if node.content:
            logger.debug("Rendering unknown ADF container: type=%s", kind)
            return children()
        logger.debug("Ignoring unsupported leaf ADF node: type=%s", kind)
        return node.text or ""

    def _render_list(self, node: ADFNode, context: _RenderContext, depth: int) -> str:
        ordered = node.type == "orderedList"
        start = self._bounded_int(node.attrs.get("order"), default=1, low=1, high=1_000_000)
        output: list[str] = []
        for offset, item in enumerate(node.content):
            item_context = _RenderContext(
                list_depth=context.list_depth,
                ordered_index=start + offset if ordered else None,
            )
            output.append(self._render_node(item, item_context, depth + 1))
        return "".join(output) + ("\n" if context.list_depth == 0 else "")

    def _render_list_item(self, node: ADFNode, context: _RenderContext, depth: int) -> str:
        marker = f"{context.ordered_index}." if context.ordered_index is not None else "-"
        indent = "  " * context.list_depth
        body_parts: list[str] = []
        nested_parts: list[str] = []
        for child in node.content:
            if child.type in {"bulletList", "orderedList"}:
                nested_context = _RenderContext(list_depth=context.list_depth + 1)
                nested_parts.append(self._render_node(child, nested_context, depth + 1))
            else:
                body_parts.append(self._render_node(child, context, depth + 1))
        body = self._normalize_blocks("".join(body_parts)).strip()
        body_lines = body.splitlines() or [""]
        first = f"{indent}{marker} {body_lines[0]}".rstrip()
        continuation = [f"{indent}  {line}".rstrip() for line in body_lines[1:]]
        return "\n".join([first, *continuation]) + "\n" + "".join(nested_parts)

    def _render_table(self, node: ADFNode, depth: int) -> str:
        """Flatten an ADF table into Markdown's rectangular table model.

        Markdown cannot represent merged cells. Colspan and rowspan cells are expanded
        across their occupied grid positions so their text remains visible, but the
        original visual merge is intentionally not preserved.
        """
        grid: list[list[tuple[str, str] | None]] = []
        logical_row = 0
        for row_node in node.content:
            if row_node.type != "tableRow":
                continue
            row_align = self._alignment(row_node.attrs.get("align"))
            cells: list[tuple[str, str, int, int]] = []
            for cell_node in row_node.content:
                if cell_node.type not in {"tableCell", "tableHeader"}:
                    continue
                text = self._normalize_blocks(
                    self._render_nodes(cell_node.content, _RenderContext(), depth + 2)
                ).strip()
                cells.append(
                    (
                        self._escape_table_cell(text),
                        self._alignment(cell_node.attrs.get("align"), default=row_align),
                        self._span(cell_node.attrs.get("colspan")),
                        self._span(cell_node.attrs.get("rowspan")),
                    )
                )
            if cells:
                self._place_table_row(grid, logical_row, cells)
                logical_row += 1
        if not grid:
            return ""
        width = max(len(row) for row in grid)
        normalized = [
            [(cell or ("", "none")) for cell in row] + [("", "none")] * (width - len(row))
            for row in grid
        ]
        header = normalized[0]
        separators = [self._alignment_marker(align) for _, align in header]
        lines = [
            self._markdown_row([text for text, _ in header]),
            self._markdown_row(separators),
        ]
        lines.extend(self._markdown_row([text for text, _ in row]) for row in normalized[1:])
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _place_table_row(
        grid: list[list[tuple[str, str] | None]],
        row_index: int,
        cells: list[tuple[str, str, int, int]],
    ) -> None:
        while len(grid) <= row_index:
            grid.append([])
        column = 0
        for text, align, colspan, rowspan in cells:
            while any(
                candidate < len(grid[row_index]) and grid[row_index][candidate] is not None
                for candidate in range(column, column + colspan)
            ):
                column += 1
            for row_offset in range(rowspan):
                target_row = row_index + row_offset
                while len(grid) <= target_row:
                    grid.append([])
                target = grid[target_row]
                required = column + colspan
                if len(target) < required:
                    target.extend([None] * (required - len(target)))
                for column_offset in range(colspan):
                    target[column + column_offset] = (text, align)
            column += colspan

    @staticmethod
    def _alignment(value: object, *, default: str = "none") -> str:
        return value if isinstance(value, str) and value in {"left", "center", "right"} else default

    @staticmethod
    def _alignment_marker(alignment: str) -> str:
        return {"left": ":---", "center": ":---:", "right": "---:"}.get(alignment, "---")

    @staticmethod
    def _span(value: object) -> int:
        if isinstance(value, bool):
            return 1
        try:
            return max(1, int(str(value)))
        except (TypeError, ValueError):
            return 1

    def _render_container(self, node: ADFNode, context: _RenderContext, depth: int) -> str:
        body = self._normalize_blocks(self._render_nodes(node.content, context, depth + 1)).strip()
        if not body:
            return ""
        title = self._string(node.attrs.get("title"))
        panel_type = self._string(node.attrs.get("panelType"))
        label = title or panel_type.title()
        prefix = f"**{label}:**\n\n" if label else ""
        quoted = "\n".join(f"> {line}" if line else ">" for line in body.splitlines())
        return f"{prefix}{quoted}\n\n"

    def _render_media(self, node: ADFNode, context: _RenderContext, depth: int) -> str:
        if node.type not in {"media", "mediaInline"}:
            parts = [self._render_node(child, context, depth + 1).strip() for child in node.content]
            body = "\n".join(part for part in parts if part)
            return f"{body}\n\n" if body else ""
        attrs = node.attrs
        alt = self._string(attrs.get("alt")) or self._string(attrs.get("name")) or "media"
        url = self._string(attrs.get("url")) or self._string(attrs.get("src"))
        media_id = self._string(attrs.get("id"))
        if url:
            return f"![{self._escape_brackets(alt)}]({url})"
        return f"[{alt} (attachment {media_id})]" if media_id else f"[{alt}]"

    def _render_action_item(self, node: ADFNode, context: _RenderContext, depth: int) -> str:
        state = self._string(node.attrs.get("state")).upper()
        marker = ("[x]" if state == "DONE" else "[ ]") if node.type == "taskItem" else "Decision:"
        body = self._normalize_blocks(self._render_nodes(node.content, context, depth + 1)).strip()
        return f"{'  ' * max(context.list_depth - 1, 0)}- {marker} {body}\n"

    def _render_extension(self, node: ADFNode, context: _RenderContext, depth: int) -> str:
        attrs = node.attrs
        title = self._string(attrs.get("text")) or self._string(attrs.get("extensionKey"))
        body = self._normalize_blocks(self._render_nodes(node.content, context, depth + 1)).strip()
        if title and body:
            return f"**{title}**\n\n{body}\n\n"
        if body:
            return f"{body}\n\n"
        return f"[{title}]" if title else ""

    def _apply_marks(self, text: str, marks: list[ADFMark]) -> str:
        rendered = text
        for mark in marks:
            kind = mark.type
            if kind == "link":
                href = self._string(mark.attrs.get("href"))
                rendered = f"[{rendered}]({href})" if href else rendered
            elif kind == "strong":
                rendered = f"**{rendered}**"
            elif kind == "em":
                rendered = f"*{rendered}*"
            elif kind == "strike":
                rendered = f"~~{rendered}~~"
            elif kind == "code":
                fence = self._inline_code_fence(rendered)
                rendered = f"{fence}{rendered}{fence}"
            elif kind == "underline":
                rendered = f"<u>{rendered}</u>"
            elif kind == "subsup":
                tag = "sup" if mark.attrs.get("type") == "sup" else "sub"
                rendered = f"<{tag}>{rendered}</{tag}>"
        return rendered

    def _plain_nodes(self, nodes: list[ADFNode], depth: int) -> list[str]:
        if depth > self._max_depth:
            raise ADFParseError("ADF maximum nesting depth exceeded")
        output: list[str] = []
        for node in nodes:
            if node.type == "text" and node.text:
                output.append(node.text)
            elif node.type == "hardBreak":
                output.append("\n")
            elif node.content:
                output.extend(self._plain_nodes(node.content, depth + 1))
        return output

    def _plain_join(self, nodes: list[ADFNode], depth: int) -> str:
        return "".join(self._plain_nodes(nodes, depth))

    @classmethod
    def _normalize_blocks(cls, markdown: str) -> str:
        """Normalize block spacing without modifying fenced code content."""
        output: list[str] = []
        open_fence: str | None = None
        for line in markdown.split("\n"):
            if open_fence is not None:
                output.append(line)
                if cls._is_fence_close(line, open_fence):
                    open_fence = None
                continue
            opening = cls._opening_fence(line)
            if opening is not None:
                open_fence = opening
                output.append(line.rstrip())
                continue
            normalized = line.rstrip()
            if normalized or not output or output[-1] != "":
                output.append(normalized)
        while output and not output[0]:
            output.pop(0)
        while output and not output[-1]:
            output.pop()
        return "\n".join(output)

    def _optimize_markdown_for_llm(self, markdown: str) -> str:
        lines: list[str] = []
        open_fence: str | None = None
        for line in markdown.splitlines():
            if open_fence is not None:
                lines.append(line)
                if self._is_fence_close(line, open_fence):
                    open_fence = None
                continue
            opening = self._opening_fence(line)
            if opening is not None:
                open_fence = opening
            else:
                line = re.sub(r"</?(?:span|div)(?:\s[^>]*)?>", "", line)
            lines.append(line.rstrip())
        return self._normalize_blocks("\n".join(lines))

    @staticmethod
    def _opening_fence(line: str) -> str | None:
        match = re.fullmatch(r"(`{3,})[^`]*", line.rstrip())
        return match.group(1) if match else None

    @staticmethod
    def _is_fence_close(line: str, fence: str) -> bool:
        return line.strip() == fence

    @staticmethod
    def _render_date(value: object) -> str:
        raw = ADFParser._string(value)
        if not raw:
            return ""
        try:
            milliseconds = int(raw)
        except ValueError:
            return raw
        from datetime import UTC, datetime

        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC).date().isoformat()

    @staticmethod
    def _bounded_int(value: object, *, default: int, low: int, high: int) -> int:
        if isinstance(value, bool):
            return default
        try:
            parsed = int(str(value))
        except (TypeError, ValueError):
            return default
        return min(max(parsed, low), high)

    @staticmethod
    def _safe_token(value: object) -> str:
        return re.sub(r"[^A-Za-z0-9_+.-]", "", ADFParser._string(value))

    @staticmethod
    def _string(value: object) -> str:
        return value if isinstance(value, str) else ""

    @staticmethod
    def _code_fence(code: str) -> str:
        longest = max((len(match.group(0)) for match in re.finditer(r"`+", code)), default=0)
        return "`" * max(3, longest + 1)

    @staticmethod
    def _inline_code_fence(code: str) -> str:
        longest = max((len(match.group(0)) for match in re.finditer(r"`+", code)), default=0)
        return "`" * max(1, longest + 1)

    @staticmethod
    def _escape_table_cell(value: str) -> str:
        return value.replace("|", r"\|").replace("\n", "<br>")

    @staticmethod
    def _escape_brackets(value: str) -> str:
        return value.replace("[", r"\[").replace("]", r"\]")

    @staticmethod
    def _markdown_row(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"
