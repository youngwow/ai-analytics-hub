"""Deterministic, loss-visible preparation from collected input to AI input."""

from __future__ import annotations

import re

from ..models import RawDocument, Source
from .contracts import PreparedChunk, PreparedDocument

_BOUNDARY = re.compile(r"\n{2,}|(?<=[.!?…])\s+(?=[А-ЯA-Z0-9«])")


def chunks_with_offsets(text: str, max_chars: int) -> tuple[PreparedChunk, ...]:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if not text:
        return ()
    chunks: list[PreparedChunk] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + max_chars)
        end = hard_end
        if hard_end < len(text):
            candidates = [m.end() for m in _BOUNDARY.finditer(text, start, hard_end)]
            # Avoid pathological tiny pieces; otherwise use a visible hard split.
            if candidates and candidates[-1] - start >= max_chars // 2:
                end = candidates[-1]
        piece = text[start:end]
        chunks.append(PreparedChunk(len(chunks), piece, start, end))
        start = end
    return tuple(chunks)


def prepare_raw_document(
    material_id: str,
    raw: RawDocument,
    source: Source,
    *,
    max_chunk_chars: int = 6000,
) -> PreparedDocument:
    text = (raw.text or raw.summary or "").strip()
    warnings: list[str] = []
    completeness = "full"
    if not text:
        completeness = "unreadable"
        warnings.append("no readable text; title and attachment metadata preserved")
    if raw.attachments:
        warnings.append("attachments are not opened in MVP")
        if not text:
            completeness = "partial"
    chunks = chunks_with_offsets(text, max_chunk_chars)
    direction = source.direction.upper()
    if direction not in {"PR", "GR", "BOTH"}:
        direction = "UNKNOWN"
    return PreparedDocument(
        id=material_id,
        title=raw.title,
        text=text,
        source_name=source.name,
        source_type=source.kind,
        source_url=raw.url or None,
        published_at=raw.published_at,
        direction=direction,  # type: ignore[arg-type]
        completeness=completeness,
        warnings=tuple(warnings),
        chunks=chunks,
        source_class=source.source_class,
    )
