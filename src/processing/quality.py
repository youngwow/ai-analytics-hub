"""Quality measured against a labelled set, not against a feeling.

«Recall важнее accuracy» is only a promise until there is a number attached, so
the gold set carries reference `type` and `priority` for every document and the
run reports recall on `high` next to the target from the spec (>= 0.95).

Documents whose substance sits in an attachment are counted as skipped rather
than as model errors: not reading files is a scope decision, not a defect.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from typing import Sequence

from ..config import ClusteringConfig

HIGH_RECALL_TARGET = 0.95
PRIORITY_ACCURACY_TARGET = 0.80
GOLD_PATH = os.path.join("tests", "fixtures", "gold", "gold_set.jsonl")
# Пары «дубль / не дубль» — расширение золотого набора под дедупликацию:
# на них калибруются порог S1 и параметры HDBSCAN (`quality --pairs`).
PAIRS_PATH = os.path.join("tests", "fixtures", "gold", "duplicate_pairs.jsonl")


@dataclass
class GoldRow:
    url: str = ""
    document_id: int | None = None
    type: str = ""
    priority: str = ""
    summary: str = ""
    skip: bool = False  # substance lives in an attachment: out of scope for 1.2
    note: str = ""


@dataclass
class Evaluation:
    total: int = 0
    scored: int = 0
    skipped: int = 0
    missing: int = 0
    type_hits: int = 0
    priority_hits: int = 0
    high_total: int = 0
    high_found: int = 0
    mistakes: list[dict] = field(default_factory=list)

    @property
    def type_accuracy(self) -> float:
        return self.type_hits / self.scored if self.scored else 0.0

    @property
    def priority_accuracy(self) -> float:
        return self.priority_hits / self.scored if self.scored else 0.0

    @property
    def high_recall(self) -> float:
        return self.high_found / self.high_total if self.high_total else 1.0

    @property
    def passed(self) -> bool:
        return (
            self.high_recall >= HIGH_RECALL_TARGET
            and self.priority_accuracy >= PRIORITY_ACCURACY_TARGET
        )

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "scored": self.scored,
            "skipped": self.skipped,
            "missing": self.missing,
            "type_accuracy": round(self.type_accuracy, 3),
            "priority_accuracy": round(self.priority_accuracy, 3),
            "high_recall": round(self.high_recall, 3),
            "passed": self.passed,
        }


def load_gold(path: str = GOLD_PATH) -> list[GoldRow]:
    """Read the labelled set; a missing file is an empty set, not a crash."""
    if not os.path.exists(path):
        return []
    rows: list[GoldRow] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            rows.append(
                GoldRow(
                    url=payload.get("url", ""),
                    document_id=payload.get("document_id"),
                    type=payload.get("type", ""),
                    priority=payload.get("priority", ""),
                    summary=payload.get("summary", ""),
                    skip=bool(payload.get("skip")),
                    note=payload.get("note", ""),
                )
            )
    return rows


def evaluate(db, rows: list[GoldRow]) -> Evaluation:
    """Compare stored cards with the reference labels."""
    report = Evaluation(total=len(rows))
    for row in rows:
        if row.skip:
            report.skipped += 1
            continue
        item = _item_for(db, row)
        if item is None:
            report.missing += 1
            if row.priority == "high":
                report.high_total += 1  # a card we never made is a missed `high`
            continue
        report.scored += 1
        if row.type and item["type"] == row.type:
            report.type_hits += 1
        if row.priority:
            if item["priority"] == row.priority:
                report.priority_hits += 1
            else:
                report.mistakes.append(
                    {
                        "url": row.url,
                        "expected": row.priority,
                        "got": item["priority"],
                        "reasoning": item["reasoning"],
                    }
                )
            if row.priority == "high":
                report.high_total += 1
                if item["priority"] == "high":
                    report.high_found += 1
    return report


def _item_for(db, row: GoldRow):
    document_id = row.document_id
    if document_id is None and row.url:
        document_id = db.documents.find_by_url(row.url)
    if document_id is None:
        return None
    return db.conn.execute(
        "SELECT i.* FROM items i JOIN item_sources s ON s.item_id = i.id WHERE s.document_id = ?",
        (document_id,),
    ).fetchone()


# ── пары «дубль / не дубль»: калибровка дедупликации ───────────────────────


@dataclass
class PairRow:
    """Две публикации и вердикт разметчика: одно ли это событие."""

    id: str
    left_title: str
    left_text: str
    right_title: str
    right_text: str
    duplicate: bool
    kind: str = ""  # reprint | paraphrase | same_topic | unrelated — для отчёта
    note: str = ""

    @property
    def texts(self) -> tuple[str, str]:
        return (
            f"{self.left_title}\n{self.left_text}".strip(),
            f"{self.right_title}\n{self.right_text}".strip(),
        )


def load_pairs(path: str = PAIRS_PATH) -> list[PairRow]:
    """Прочитать набор пар; нет файла — пустой список, а не падение."""
    if not os.path.exists(path):
        return []
    rows: list[PairRow] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            left, right = payload.get("left") or {}, payload.get("right") or {}
            rows.append(
                PairRow(
                    id=str(payload.get("id", len(rows) + 1)),
                    left_title=left.get("title", ""),
                    left_text=left.get("text", ""),
                    right_title=right.get("title", ""),
                    right_text=right.get("text", ""),
                    duplicate=bool(payload.get("duplicate")),
                    kind=payload.get("kind", ""),
                    note=payload.get("note", ""),
                )
            )
    return rows


def _pr(hits: int, flagged: int, positives: int) -> tuple[float, float]:
    recall = hits / positives if positives else 1.0
    precision = hits / flagged if flagged else 1.0
    return round(recall, 3), round(precision, 3)


def evaluate_pairs(
    embedder,
    pairs: Sequence[PairRow],
    *,
    clustering: ClusteringConfig,
    threshold: float,
    grid: bool = False,
) -> dict:
    """Что дедупликация делает с размеченными парами.

    S1: косинус каждой пары против `cosine_threshold` плюс порог, лучший по F1.
    Кластеризация: все тексты пар кладутся в один пул, и пара считается
    «предложенной», если обе её стороны попали в одну группу HDBSCAN. При `grid`
    перебираются `min_cluster_size`, `min_samples`, снижение и метод выбора.
    """
    from ..processing import clustering as clustering_mod
    from ..processing.embeddings import as_tensor

    # Один текст может участвовать в нескольких парах (дубль / та же тема /
    # не о том): в пул он кладётся один раз, иначе его копии образуют «группу».
    index: dict[str, int] = {}
    sides: list[tuple[int, int]] = []
    for pair in pairs:
        left, right = pair.texts
        sides.append((index.setdefault(left, len(index)), index.setdefault(right, len(index))))
    texts = list(index)
    matrix = as_tensor(embedder, texts)
    positives = sum(1 for pair in pairs if pair.duplicate)

    sims = [float(matrix[left] @ matrix[right]) for left, right in sides]
    hits = sum(1 for pair, sim in zip(pairs, sims) if pair.duplicate and sim >= threshold)
    flagged = sum(1 for sim in sims if sim >= threshold)
    recall, precision = _pr(hits, flagged, positives)
    duplicate_sims = [sim for pair, sim in zip(pairs, sims) if pair.duplicate]
    other_sims = [sim for pair, sim in zip(pairs, sims) if not pair.duplicate]
    s1 = {
        "threshold": threshold,
        "recall": recall,
        "precision": precision,
        "min_duplicate": round(min(duplicate_sims), 3) if duplicate_sims else None,
        "max_non_duplicate": round(max(other_sims), 3) if other_sims else None,
        "best_threshold": _best_threshold(pairs, sims),
    }

    variants = [clustering]
    if grid:
        variants = [
            replace(
                clustering,
                min_cluster_size=size,
                min_samples=samples,
                reduction=reduction,
                cluster_selection_method=method,
            )
            for size in (2, 3, 5)
            for samples in (1, 2, 5)
            for reduction in ("none", "pca", "umap")
            for method in ("leaf", "eom")
            if samples <= size
        ]
    ids = list(range(len(texts)))
    rows = []
    misses: list[str] = []
    for variant in variants:
        try:
            groups = clustering_mod.find_groups(ids, matrix, variant)
        except RuntimeError as e:  # umap не установлен и т. п.
            rows.append({**_variant_fields(variant), "error": str(e)[:80]})
            continue
        label_of: dict[int, int] = {}
        for group in groups:
            for member in group.item_ids:
                label_of[member] = group.label
        together = [
            label_of.get(left) is not None and label_of.get(left) == label_of.get(right)
            for left, right in sides
        ]
        hits = sum(1 for pair, same in zip(pairs, together) if pair.duplicate and same)
        flagged = sum(1 for same in together if same)
        recall, precision = _pr(hits, flagged, positives)
        current = variant == clustering
        rows.append(
            {
                **_variant_fields(variant),
                "recall": recall,
                "precision": precision,
                "groups": len(groups),
                "current": current,
            }
        )
        if current:
            for pair, same, sim in zip(pairs, together, sims):
                if pair.duplicate and not same:
                    misses.append(f"пропущен дубль {pair.id} ({pair.kind}, косинус {sim:.3f})")
                elif same and not pair.duplicate:
                    misses.append(f"ложная группа {pair.id} ({pair.kind}, косинус {sim:.3f})")
    model = getattr(embedder, "model_name", "") or type(embedder).__name__
    return {
        "pairs": len(pairs),
        "duplicates": positives,
        "model": model,
        "similarities": [round(sim, 4) for sim in sims],
        "s1": s1,
        "clustering": rows,
        "misses": misses,
    }


def _variant_fields(config: ClusteringConfig) -> dict:
    return {
        "min_cluster_size": config.min_cluster_size,
        "min_samples": config.min_samples,
        "reduction": config.reduction,
        "cluster_selection_method": config.cluster_selection_method,
    }


def _best_threshold(pairs: Sequence[PairRow], sims: Sequence[float]) -> float | None:
    """Порог S1 с максимальным F1 на парах (при равенстве — более консервативный)."""
    positives = sum(1 for pair in pairs if pair.duplicate)
    if not positives or not sims:
        return None
    best, best_f1 = None, -1.0
    for candidate in sorted({round(sim, 3) for sim in sims}, reverse=True):
        hits = sum(1 for pair, sim in zip(pairs, sims) if pair.duplicate and sim >= candidate)
        flagged = sum(1 for sim in sims if sim >= candidate)
        recall, precision = _pr(hits, flagged, positives)
        f1 = 2 * recall * precision / (recall + precision) if recall + precision else 0.0
        if f1 > best_f1:
            best, best_f1 = candidate, f1
    return best
