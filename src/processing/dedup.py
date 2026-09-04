"""S1 — deduplication and clustering.

Three steps, cheapest first (research.md, R-04): exact hash, then SimHash over
shingles, then cosine over embeddings — but only among candidates the first two
steps left standing. That blocking is what keeps a pure-Python cosine fast
enough to need neither numpy nor a vector index.
"""

from __future__ import annotations

import hashlib
import math
import re
from array import array
from dataclasses import dataclass
from typing import Iterable, Sequence

_TOKEN_RE = re.compile(r"[А-Яа-яЁёA-Za-z0-9]+")
_BITS = 64
# Reprints repeat the same facts; commentary adds a speaker. Two texts about one
# event that both carry a speaker are positions, not copies — the spec wants those
# kept visible rather than collapsed into one card.
_OPINION_RE = re.compile(
    r"\b(заяви|счита|подчеркну|раскритикова|выступил против|поддержа|по мнению|отмети|"
    r"прокомментирова|предложи|потребова|возрази)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Match:
    """An existing card a new document should join."""

    item_id: int
    cluster_id: int
    score: float
    reason: str


def tokens(text: str, shingle: int = 3) -> list[str]:
    """Word 3-grams: robust to a reordered sentence, unlike single words."""
    words = [w.lower() for w in _TOKEN_RE.findall(text)]
    if len(words) < shingle:
        return words
    return [" ".join(words[i : i + shingle]) for i in range(len(words) - shingle + 1)]


def simhash(text: str) -> str:
    """64-bit SimHash as hex; empty text has no hash (an empty string, not a zero)."""
    grams = tokens(text)
    if not grams:
        return ""
    vector = [0] * _BITS
    for gram in grams:
        digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(_BITS):
            vector[bit] += 1 if value >> bit & 1 else -1
    result = 0
    for bit in range(_BITS):
        if vector[bit] > 0:
            result |= 1 << bit
    return f"{result:016x}"


def hamming(left: str, right: str) -> int:
    """Bit distance between two SimHashes; unknown hashes are infinitely far."""
    if not left or not right:
        return _BITS + 1
    try:
        return bin(int(left, 16) ^ int(right, 16)).count("1")
    except ValueError:
        return _BITS + 1


def encode_vector(values: Sequence[float]) -> bytes:
    return array("f", [float(v) for v in values]).tobytes()


def decode_vector(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    buffer = array("f")
    try:
        buffer.frombytes(blob)
    except ValueError:
        return []
    return list(buffer)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


def centroid(vectors: Iterable[Sequence[float]]) -> list[float]:
    rows = [v for v in vectors if v]
    if not rows:
        return []
    width = len(rows[0])
    if any(len(v) != width for v in rows):
        return list(rows[0])
    return [sum(v[i] for v in rows) / len(rows) for i in range(width)]


def divergent(left: str, right: str) -> bool:
    """Do these two publications voice positions rather than repeat one fact?"""
    return bool(_OPINION_RE.search(left or "")) and bool(_OPINION_RE.search(right or ""))


def find_match(
    candidates: Iterable,
    *,
    text_simhash: str,
    embedding: Sequence[float] | None = None,
    max_distance: int = 8,
    threshold: float = 0.86,
) -> Match | None:
    """The card this document belongs to, or None to start a new one.

    `candidates` are rows from `documents.clustered_candidates`. NPA cards are
    never joined here: their identity is the act number, decided by the service.
    """
    best: Match | None = None
    for row in candidates:
        if row["type"] == "npa":
            continue
        distance = hamming(text_simhash, row["simhash"] or "")
        if distance <= max_distance:
            score = 1.0 - distance / _BITS
            if best is None or score > best.score:
                best = Match(row["item_id"], row["cluster_id"], score, f"simhash d={distance}")
            continue
        if embedding:
            other = decode_vector(row["embedding"])
            if not other:
                continue
            score = cosine(embedding, other)
            if score >= threshold and (best is None or score > best.score):
                best = Match(row["item_id"], row["cluster_id"], score, f"cosine {score:.3f}")
    return best
