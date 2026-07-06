"""Section-aware chunking.

Contract: chunks follow Markdown heading boundaries so a citation's ``heading`` points
at a real section a judge can open and verify. Oversized sections are split with a small
line overlap. The document's title + breadcrumb are prepended to the first chunk's
embedded text (not its stored content) so hierarchy context enters the vector space —
the sibling project validated this effect; here we apply it per-chunk cheaply by
embedding "context header + content" while storing clean content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from databridge.ingest.markdown import SourceDocument

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_DEFAULT_MAX_CHARS = 1800
_OVERLAP_LINES = 2


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: str
    source_id: str
    space_key: str
    title: str
    heading: str | None
    breadcrumb: str | None
    content: str
    seq: int

    @property
    def embedding_text(self) -> str:
        """Text sent to the embedder — content plus hierarchy context header."""
        context_parts = [p for p in (self.breadcrumb, self.title, self.heading) if p]
        header = " > ".join(context_parts)
        return f"[{header}]\n{self.content}" if header else self.content


def chunk_document(doc: SourceDocument, *, max_chars: int = _DEFAULT_MAX_CHARS) -> list[Chunk]:
    sections = _split_sections(doc.body)
    chunks: list[Chunk] = []
    seq = 0
    for heading, lines in sections:
        for piece in _split_oversized(lines, max_chars=max_chars):
            content = "\n".join(piece).strip()
            if not content:
                continue
            chunks.append(
                Chunk(
                    chunk_id=f"{doc.source_id}#{seq}",
                    source_id=doc.source_id,
                    space_key=doc.space_key,
                    title=doc.title,
                    heading=heading,
                    breadcrumb=doc.breadcrumb,
                    content=content,
                    seq=seq,
                )
            )
            seq += 1
    return chunks


def _split_sections(body: str) -> list[tuple[str | None, list[str]]]:
    sections: list[tuple[str | None, list[str]]] = []
    current_heading: str | None = None
    current_lines: list[str] = []
    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            if current_lines or current_heading is not None:
                sections.append((current_heading, current_lines))
            current_heading = match.group(2).strip()
            current_lines = [line]
        else:
            current_lines.append(line)
    sections.append((current_heading, current_lines))
    return [(h, lines) for h, lines in sections if any(ln.strip() for ln in lines)]


def _split_oversized(lines: list[str], *, max_chars: int) -> list[list[str]]:
    total = sum(len(ln) + 1 for ln in lines)
    if total <= max_chars:
        return [lines]
    pieces: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > max_chars and current:
            pieces.append(current)
            current = current[-_OVERLAP_LINES:] if _OVERLAP_LINES else []
            size = sum(len(ln) + 1 for ln in current)
        current.append(line)
        size += len(line) + 1
    if current:
        pieces.append(current)
    return pieces
