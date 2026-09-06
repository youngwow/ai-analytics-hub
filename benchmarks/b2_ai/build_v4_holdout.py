#!/usr/bin/env python3
"""Create the post-freeze B2 v4 holdout from unused B1 observations."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
V3 = HERE / "data" / "v3"
V4 = HERE / "data" / "v4"
REAL_IDS = (31, 37, 39, 40, 42, 43, 44, 45, 49, 53, 58, 61, 69, 76, 78, 82, 86, 87, 91, 94, 98, 103, 112, 113)


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")


def placeholder(item_id: str, text: str) -> dict:
    evidence = text[: min(500, len(text))]
    return {
        "id": item_id,
        "relevance": "unknown",
        "roles": [],
        "importance": "medium",
        "critical_or_escalate": False,
        "topic": "pending_blind_adjudication",
        "impact_on_gs_labs": "pending blind adjudication",
        "impact_confidence": "unknown",
        "must_facts": [{"fact_id": f"{item_id}-f1", "claim": evidence, "evidence": evidence}],
        "forbidden_claims": [],
        "allowed_ambiguity": "All label fields are unscored until two blind judges agree.",
        "truth_sources": ["B1 frozen live capture"],
        "scored_fields": [],
    }


def main() -> int:
    if V4.exists():
        shutil.rmtree(V4)
    shutil.copytree(V3, V4)
    latest = (ROOT / "benchmarks/b1_technical/runs/LATEST_RUN").read_text(encoding="utf-8").strip()
    b1_path = ROOT / "benchmarks/b1_technical/runs" / latest / "documents.jsonl"
    b1 = {int(row["id"]): row for row in load_jsonl(b1_path)}
    materials = load_jsonl(V4 / "materials.jsonl")
    catalog = load_jsonl(V4 / "catalog.jsonl")
    truth = load_jsonl(V4 / "ground_truth.jsonl")
    event_cases = load_jsonl(V4 / "event_cases.jsonl")
    holdout_ids = []
    for number in REAL_IDS:
        source = b1[number]
        item_id = f"h4-{number:03d}"
        text = str(source.get("text") or source.get("summary") or source.get("title") or "")
        materials.append(
            {
                "id": item_id,
                "title": source.get("title") or item_id,
                "text": text,
                "source_name": source.get("source_name") or "",
                "source_type": source.get("source_kind") or "",
                "source_url": source.get("url"),
                "published_at": source.get("published_at"),
                "language": "ru",
            }
        )
        catalog.append({"id": item_id, "split": "holdout", "layer": "real-flow", "provenance_kind": "b1_live_capture_post_prompt_freeze", "anchor": f"B1:{latest}:document:{number}", "sets": ["B2-F", "B2-R", "B2-E"], "diagnostic_risks": ["fresh_holdout"], "synthetic": False})
        truth.append(placeholder(item_id, text))
        holdout_ids.append(item_id)

    variants = [
        ("h4-e42", "Операторы предлагают распределить новые дата-центры по регионам", "Участники рынка предложили размещать новые мощности ЦОД за пределами Москвы из-за дефицита площадок и электроэнергии.", "h4-042"),
        ("h4-e43", "Комментарий к постановлению № 1125", "Отраслевой канал пересказал опубликованное постановление Правительства РФ от 03.09.2026 № 1125. Юридический статус определяется официальной публикацией.", "h4-043"),
        ("h4-e113", "Контур.Толк добавил ИИ-функции", "Сервис видеоконференций «Контур.Толк» представил ИИ-функции для совместной работы над проектами.", "h4-113"),
    ]
    for item_id, title, text, target in variants:
        materials.append({"id": item_id, "title": title, "text": text, "source_name": "Контрольный вторичный источник", "source_type": "telegram", "source_url": None, "published_at": "2026-09-04T12:00:00+03:00", "language": "ru"})
        catalog.append({"id": item_id, "split": "holdout", "layer": "challenge", "provenance_kind": "post_freeze_event_variant", "anchor": target, "sets": ["B2-F", "B2-R", "B2-E"], "diagnostic_risks": ["paraphrase_event_link"], "synthetic": True})
        truth.append(placeholder(item_id, text))
        holdout_ids.append(item_id)
    paired = {target: variant for variant, _, _, target in variants}
    used = set(paired) | set(paired.values())
    expected = [[target, paired[target]] for target in paired]
    expected.extend([[item_id] for item_id in holdout_ids if item_id not in used])
    event_cases.append({"case_id": "H4-FRESH-HOLDOUT", "split": "holdout", "member_ids": holdout_ids, "expected_clusters": expected, "preserve_positions": True, "failure_to_avoid": "False merge diverse real-flow items or miss three explicit paraphrase updates."})
    write_jsonl(V4 / "materials.jsonl", materials)
    write_jsonl(V4 / "catalog.jsonl", catalog)
    write_jsonl(V4 / "ground_truth.jsonl", truth)
    write_jsonl(V4 / "event_cases.jsonl", event_cases)
    contract = json.loads((V4 / "contract.json").read_text(encoding="utf-8"))
    contract.update({"dataset_version": "v4.0.0-provisional", "created_at": "2026-09-06T17:00:00+03:00", "freeze_state": "post_prompt_freeze_holdout_inputs_frozen_labels_pending_blind_agreement", "holdout_source": "24 unused B1 observations plus 3 post-freeze event variants"})
    (V4 / "contract.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    coverage = json.loads((V4 / "coverage.json").read_text(encoding="utf-8"))
    coverage["materials_total"] = len(materials)
    coverage["by_split"] = dict(Counter(row["split"] for row in catalog))
    coverage["fresh_holdout"] = {"materials": len(holdout_ids), "real": len(REAL_IDS), "synthetic_event_variants": len(variants)}
    (V4 / "coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    checksums = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in sorted(V4.iterdir()) if path.is_file() and path.name != "checksums.sha256"]
    (V4 / "checksums.sha256").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    print(json.dumps(coverage["fresh_holdout"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
