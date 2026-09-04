"""S0 — normalisation: what the model actually reads.

`norm_text` is stored on the document because `evidence_offsets` are counted in
its coordinates: if the cleaning rules changed silently, every stored offset and
every UI highlight would drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..sources.textutil import strip_html

# Lines that carry no content: nav leftovers, social calls, photo credits.
_BOILERPLATE_RE = re.compile(
    r"^\s*(читайте также|читайте по теме|материалы по теме|подписывайтесь|подписаться|"
    r"поделиться|реклама|фото\s*:|иллюстрация\s*:|источник\s*:|теги\s*:|"
    r"все новости|смотрите также|нашли ошибку|подпишитесь)",
    re.IGNORECASE,
)
_MULTI_NL_RE = re.compile(r"\n{2,}")
_SPACES_RE = re.compile(r"[ \t ]+")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?…])\s+(?=[«\"(\[]?[А-ЯA-ZЁ0-9])")


@dataclass(frozen=True)
class Sentence:
    start: int
    end: int
    text: str


def normalize(text: str) -> str:
    """Collapse whitespace and drop boilerplate lines, preserving reading order."""
    if not text:
        return ""
    plain = strip_html(text) if "<" in text else text
    lines: list[str] = []
    for raw in plain.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _SPACES_RE.sub(" ", raw).strip()
        if not line or _BOILERPLATE_RE.match(line):
            continue
        lines.append(line)
    return _MULTI_NL_RE.sub("\n", "\n".join(lines)).strip()


def split_sentences(text: str) -> list[Sentence]:
    """Sentences with their offsets in `text` — the anchors S6 verifies against."""
    out: list[Sentence] = []
    for block_start, block in _blocks(text):
        cursor = 0
        for part in _SENTENCE_END_RE.split(block):
            stripped = part.strip()
            if not stripped:
                cursor += len(part)
                continue
            offset = block.find(stripped, cursor)
            if offset < 0:
                offset = cursor
            out.append(
                Sentence(
                    start=block_start + offset,
                    end=block_start + offset + len(stripped),
                    text=stripped,
                )
            )
            cursor = offset + len(stripped)
    return out


def _blocks(text: str) -> list[tuple[int, str]]:
    blocks, position = [], 0
    for line in text.split("\n"):
        if line.strip():
            blocks.append((position, line))
        position += len(line) + 1
    return blocks


def clip(text: str, limit: int) -> str:
    """Cut to `limit` characters on a sentence boundary where one is close by."""
    if limit <= 0 or len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "), window.rfind("\n"))
    return window[: cut + 1].strip() if cut > limit * 0.6 else window.strip()


def chunks(text: str, size: int) -> list[str]:
    """Split long documents for the map-reduce path, never mid-sentence."""
    if size <= 0 or len(text) <= size:
        return [text] if text else []
    out: list[str] = []
    current = ""
    for sentence in split_sentences(text) or [Sentence(0, len(text), text)]:
        if current and len(current) + len(sentence.text) + 1 > size:
            out.append(current.strip())
            current = ""
        current += sentence.text + " "
    if current.strip():
        out.append(current.strip())
    return out


def lead(text: str, count: int = 3) -> list[str]:
    """First sentences — the extractive baseline used when the model is down."""
    return [s.text for s in split_sentences(text)[:count]]
