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
from dataclasses import dataclass, field

HIGH_RECALL_TARGET = 0.95
PRIORITY_ACCURACY_TARGET = 0.80
GOLD_PATH = os.path.join("tests", "fixtures", "gold", "gold_set.jsonl")


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
