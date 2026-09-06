"""S1 — deduplication and clustering.

Three steps, cheapest first (research.md, R-04): exact hash, then SimHash over
shingles, then cosine over embeddings — but only among candidates the first two
steps left standing.

Косинусы считает numpy одним умножением матрицы на вектор: блокировка по SimHash
отсеивает не всё, поэтому на окне в 2000 кандидатов и прогоне в 200 документов
набегает 400 тысяч сравнений по 768 чисел. В чистом Python это была самая дорогая
часть прогона; матрица кандидатов собирается один раз, строки нормированы заранее,
и на документ приходится один вызов BLAS. Векторного индекса по-прежнему не нужно:
окно кандидатов ограничено семью днями.
"""

from __future__ import annotations

import hashlib
import re
from array import array
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

from ..utils import get_logger

log = get_logger("dedup")

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


@dataclass
class Candidate:
    """Кандидат на присоединение, подготовленный один раз на прогон.

    Вектор из BLOB'а раскодирован, норма посчитана: иначе на 2000 кандидатов ×
    200 документов приходится 400 тысяч раскодирований и вдвое больше корней —
    результат-то каждый раз один и тот же.
    """

    item_id: int
    cluster_id: int
    type: str
    simhash: str
    title: str
    embedding: list[float]
    norm: float
    document_id: int | None = None

    @classmethod
    def from_row(cls, row) -> "Candidate":
        vector = decode_vector(row["embedding"])
        keys = row.keys() if hasattr(row, "keys") else ()
        return cls(
            item_id=row["item_id"],
            cluster_id=row["cluster_id"],
            type=row["type"],
            simhash=row["simhash"] or "",
            title=(row["title"] if "title" in keys else "") or "",
            embedding=vector,
            norm=vector_norm(vector),
            document_id=row["id"] if "id" in keys else None,
        )


def prepare(rows: Iterable) -> list[Candidate]:
    """Подготовить пул кандидатов: раскодировать векторы и посчитать нормы."""
    if isinstance(rows, CandidatePool):
        return rows.candidates
    return [row if isinstance(row, Candidate) else Candidate.from_row(row) for row in rows]


def pool(rows: Iterable, width: int | None = None) -> "CandidatePool":
    """Пул кандидатов с матрицей эмбеддингов — строится один раз на прогон.

    `width` — длина вектора текущей модели эмбеддингов (`embeddings.dimensions`):
    кандидаты с векторами другой длины посчитаны прежней моделью и сравниваются
    только по SimHash. Без `width` берётся преобладающая длина.
    """
    return rows if isinstance(rows, CandidatePool) else CandidatePool(prepare(rows), width=width)


class CandidatePool:
    """Кандидаты прогона плюс матрица их нормированных векторов.

    Косинусы ко всем кандидатам считаются одним умножением матрицы на вектор
    запроса. Порядок кандидатов сохраняется: на нём держится выбор лучшего
    совпадения (строго `>`, поэтому при равенстве побеждает первый).
    """

    def __init__(self, candidates: list[Candidate], width: int | None = None):
        self.candidates = candidates
        usable = [
            (position, candidate)
            for position, candidate in enumerate(candidates)
            if candidate.embedding and candidate.norm
        ]
        # В базе могут лежать векторы от прежней модели другой размерности:
        # матрица из разных длин — это ValueError. Длину задаёт текущая модель
        # (`width`), а без неё — преобладающая. Остальные не сравниваются вовсе —
        # ровно как раньше, когда косинус разноразмерных векторов давал 0.0 и до
        # порога не доходил; но теперь об этом сказано в логе.
        width = width or _dominant_width([c for _, c in usable])
        rows = [(position, c) for position, c in usable if len(c.embedding) == width]
        skipped = len(usable) - len(rows)
        if skipped:
            log.warning(
                "%d кандидатов с векторами не длины %d (прежняя модель эмбеддингов) "
                "сравниваются только по SimHash",
                skipped,
                width,
            )
        self._positions = np.array([position for position, _ in rows], dtype=np.intp)
        if rows:
            matrix = np.array([c.embedding for _, c in rows], dtype=np.float64)
            # Строки нормируются заранее: тогда косинус — это просто скалярное
            # произведение на нормированный вектор запроса.
            self._matrix = matrix / np.array([[c.norm] for _, c in rows], dtype=np.float64)
        else:
            self._matrix = np.empty((0, 0), dtype=np.float64)

    def __iter__(self):
        return iter(self.candidates)

    def __len__(self) -> int:
        return len(self.candidates)

    def scores(self, embedding: Sequence[float] | None) -> np.ndarray | None:
        """Косинусы ко всем кандидатам одним умножением; `None`, если считать нечего."""
        if embedding is None or len(embedding) == 0 or not len(self._positions):
            return None
        query = np.asarray(embedding, dtype=np.float64)
        norm = float(np.linalg.norm(query))
        if not norm or query.shape[0] != self._matrix.shape[1]:
            return None
        result = np.zeros(len(self.candidates), dtype=np.float64)
        result[self._positions] = self._matrix @ (query / norm)
        return result


def _dominant_width(candidates: list[Candidate]) -> int:
    """Самая частая размерность вектора среди кандидатов (0, если их нет)."""
    counts: dict[int, int] = {}
    for candidate in candidates:
        width = len(candidate.embedding)
        counts[width] = counts.get(width, 0) + 1
    return max(counts, key=lambda width: (counts[width], width)) if counts else 0


def vector_norm(vector: Sequence[float]) -> float:
    if vector is None or len(vector) == 0:
        return 0.0
    return float(np.linalg.norm(np.asarray(vector, dtype=np.float64)))


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
    """Косинус пары векторов. Пакетный путь считает то же самое матрицей."""
    if left is None or right is None or len(left) == 0 or len(right) == 0:
        return 0.0
    if len(left) != len(right):
        return 0.0
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    return cosine_prepared(a, float(np.linalg.norm(a)), b, float(np.linalg.norm(b)))


def cosine_prepared(
    left: Sequence[float], left_norm: float, right: Sequence[float], right_norm: float
) -> float:
    """Косинус с заранее посчитанными нормами — то же число, вдвое меньше работы."""
    if left is None or right is None or len(left) == 0 or len(right) == 0:
        return 0.0
    if len(left) != len(right) or not left_norm or not right_norm:
        return 0.0
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    return float(a @ b) / (left_norm * right_norm)


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
    prepared = pool(candidates)
    # Все косинусы сразу: дальше остаётся обычный обход в исходном порядке.
    scores = prepared.scores(embedding)
    for position, candidate in enumerate(prepared.candidates):
        if candidate.type == "npa":
            continue
        distance = hamming(text_simhash, candidate.simhash)
        if distance <= max_distance:
            score = 1.0 - distance / _BITS
            if best is None or score > best.score:
                best = Match(
                    candidate.item_id, candidate.cluster_id, score, f"simhash d={distance}"
                )
            continue
        if scores is not None and candidate.embedding:
            score = float(scores[position])
            if score >= threshold and (best is None or score > best.score):
                best = Match(
                    candidate.item_id, candidate.cluster_id, score, f"cosine {score:.3f}"
                )
    return best
