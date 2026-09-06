#!/usr/bin/env python3
"""Build B2 v2 from frozen v1 plus a larger real-flow layer from B1."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
V1 = HERE / "data" / "v1"
V2 = HERE / "data" / "v2"
TRUTH = (
    ROOT
    / "project/ЦИФРА/02_НАЙТИ_И_ПРОВЕРИТЬ_СТАВКУ/03_СРЕДА_ЭКСПЕРИМЕНТОВ/CONTEXT_TRUTH_V1"
)

# Human-reviewed labels for a stratified slice of the real B1 capture.  These
# labels are deliberately broad; nuanced content quality is judged separately.
LABELS = {
    7: ("borderline", "medium", ["PR"], "technology_market"),
    16: ("borderline", "medium", ["PR"], "technology_market"),
    70: ("irrelevant", "low", [], "unrelated_support"),
    75: ("relevant", "high", ["PR", "GR", "HEAD"], "telecom_regulation"),
    77: ("relevant", "high", ["GR", "HEAD"], "software_support"),
    97: ("relevant", "high", ["GR", "HEAD"], "software_support"),
    83: ("irrelevant", "low", [], "promotional_noise"),
    92: ("relevant", "high", ["GR", "HEAD"], "digital_trust_regulation"),
    8: ("borderline", "medium", ["PR"], "regional_ai_market"),
    15: ("relevant", "medium", ["PR"], "cybersecurity_market"),
    17: ("borderline", "medium", ["PR", "HEAD"], "electronics_market"),
    25: ("borderline", "low", ["PR"], "regional_digitalization"),
    28: ("irrelevant", "low", [], "unrelated_regulation"),
    29: ("irrelevant", "low", [], "unrelated_finance_regulation"),
    79: ("relevant", "medium", ["PR"], "television_market"),
    80: ("relevant", "high", ["PR", "GR", "HEAD"], "telecom_regulation"),
    73: ("irrelevant", "low", [], "commemorative_noise"),
    84: ("relevant", "high", ["PR", "HEAD"], "electronics_market"),
    96: ("relevant", "high", ["PR", "HEAD"], "electronics_market"),
    85: ("relevant", "medium", ["PR"], "video_communications_market"),
    89: ("relevant", "high", ["PR", "GR", "HEAD"], "ai_regulation"),
    93: ("irrelevant", "low", [], "personnel_news"),
    99: ("irrelevant", "low", [], "event_promotion"),
    124: ("relevant", "high", ["GR", "HEAD"], "npa"),
}

SPLITS = {
    "development": {7, 16, 70, 75, 77, 97, 83, 92},
    "validation": {8, 15, 17, 25, 28, 29, 79, 80},
    "holdout": {73, 84, 96, 85, 89, 93, 99, 124},
}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def split_for(number: int) -> str:
    return next(name for name, ids in SPLITS.items() if number in ids)


def first_fact(text: str, title: str) -> str:
    clean = text.strip()
    for match in re.finditer(r"[^.!?]*(?:[.!?]|$)", clean, flags=re.S):
        piece = match.group(0).strip()
        if len(piece) >= 25:
            return piece[:700]
    return clean[:700] or title


def main() -> int:
    if V2.exists():
        shutil.rmtree(V2)
    V2.mkdir(parents=True)
    for name in (
        "materials.jsonl",
        "catalog.jsonl",
        "ground_truth.jsonl",
        "event_cases.jsonl",
        "npa_trajectories.jsonl",
        "npa_link_cases.jsonl",
        "rat_packages.json",
        "hypothesis_coverage.json",
    ):
        shutil.copy2(V1 / name, V2 / name)

    latest_file = ROOT / "benchmarks/b1_technical/runs/LATEST_RUN"
    b1_dir = latest_file.parent / latest_file.read_text(encoding="utf-8").strip()
    b1_rows = {int(row["id"]): row for row in read_jsonl(b1_dir / "documents.jsonl")}
    materials = read_jsonl(V2 / "materials.jsonl")
    catalog = read_jsonl(V2 / "catalog.jsonl")
    truth = read_jsonl(V2 / "ground_truth.jsonl")
    event_cases = read_jsonl(V2 / "event_cases.jsonl")
    truth_by_id = {row["id"]: row for row in truth}
    rare_risks = {
        "hidden_long_text_fact",
        "unknown_internal_scope",
        "missing_company_attribute",
        "stale_republication",
    }

    for row in catalog:
        row["layer"] = (
            "real-flow"
            if row["provenance_kind"] == "b1_live_capture"
            else "case-anchor"
            if row["provenance_kind"] == "case_owner_excerpt"
            else "rare-safety"
            if truth_by_id.get(row["id"], {}).get("critical_or_escalate")
            or rare_risks.intersection(row.get("diagnostic_risks", []))
            else "challenge"
        )

    new_ids = []
    for number, labels in LABELS.items():
        source = b1_rows[number]
        item_id = f"rf-{number:03d}"
        new_ids.append(item_id)
        text = str(source.get("text") or source.get("summary") or "")
        relevance, importance, roles, topic = labels
        material = {
            "id": item_id,
            "title": source.get("title") or item_id,
            "text": text,
            "source_name": source.get("source_name") or "",
            "source_type": source.get("source_kind") or "",
            "source_url": source.get("url"),
            "published_at": source.get("published_at"),
            "language": "ru",
        }
        materials.append(material)
        catalog.append(
            {
                "id": item_id,
                "split": split_for(number),
                "layer": "real-flow",
                "provenance_kind": "b1_live_capture",
                "anchor": f"B1:{b1_dir.name}:document:{number}",
                "sets": ["B2-F", "B2-R", "B2-E"],
                "diagnostic_risks": ["real_distribution_anchor"],
                "synthetic": False,
            }
        )
        fact = first_fact(text, str(material["title"]))
        truth.append(
            {
                "id": item_id,
                "relevance": relevance,
                "roles": roles,
                "importance": importance,
                "critical_or_escalate": False,
                "topic": topic,
                "impact_on_gs_labs": "Контекстная применимость оценивается по Context Truth Pack.",
                "impact_confidence": "medium" if relevance == "borderline" else "high",
                "must_facts": [
                    {"fact_id": f"{item_id}-f1", "claim": fact, "evidence": fact}
                ],
                "forbidden_claims": [
                    "Материал прямо относится к GS Labs, если компания не названа в оригинале.",
                    "Неофициальная публикация сама устанавливает юридический статус НПА.",
                ],
                "allowed_ambiguity": "Borderline допускает расхождение только при явном unknown.",
                "truth_sources": [f"B1:{b1_dir.name}:document:{number}", "CONTEXT_TRUTH_V1"],
            }
        )

    # Every B2-E item belongs to exactly one case. Cross-channel rediscoveries
    # are grouped; remaining real observations are explicit singletons.
    grouped = [
        ("RF-E1", "development", ["rf-007", "rf-016"]),
        ("RF-E2", "development", ["rf-077", "rf-097"]),
        ("RF-E3", "holdout", ["rf-084", "rf-096"]),
    ]
    grouped_ids = {item for _, _, members in grouped for item in members}
    for case_id, split, members in grouped:
        event_cases.append(
            {
                "case_id": case_id,
                "split": split,
                "member_ids": members,
                "expected_clusters": [members],
                "preserve_positions": False,
                "failure_to_avoid": "Оставить повторно обнаруженное событие разными объектами.",
            }
        )
    by_split: dict[str, list[str]] = {name: [] for name in SPLITS}
    for number in LABELS:
        item_id = f"rf-{number:03d}"
        if item_id not in grouped_ids:
            by_split[split_for(number)].append(item_id)
    for split, members in by_split.items():
        if members:
            event_cases.append(
                {
                    "case_id": f"RF-SINGLE-{split}",
                    "split": split,
                    "member_ids": members,
                    "expected_clusters": [[item] for item in members],
                    "preserve_positions": True,
                    "failure_to_avoid": "Склеить разные реальные материалы только по общей теме.",
                }
            )

    write_jsonl(V2 / "materials.jsonl", materials)
    write_jsonl(V2 / "catalog.jsonl", catalog)
    write_jsonl(V2 / "ground_truth.jsonl", truth)
    write_jsonl(V2 / "event_cases.jsonl", event_cases)
    shutil.copy2(TRUTH / "context.json", V2 / "context_gs_labs.json")

    counts = {
        "materials_total": len(materials),
        "new_real_flow": len(new_ids),
        "by_split": Counter(row["split"] for row in catalog),
        "by_layer": Counter(row["layer"] for row in catalog),
        "by_source_type": Counter(row["source_type"] for row in materials),
        "by_relevance": Counter(row["relevance"] for row in truth),
        "by_importance": Counter(row["importance"] for row in truth),
        "event_cases": len(event_cases),
        "npa_trajectories": len(read_jsonl(V2 / "npa_trajectories.jsonl")),
    }
    (V2 / "coverage.json").write_text(
        json.dumps(counts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    contract = json.loads((V1 / "contract.json").read_text(encoding="utf-8"))
    contract.update(
        {
            "dataset_version": "v2.0.0",
            "created_at": "2026-09-06T15:00:00+03:00",
            "scope": "Four-layer diagnostic basket: real-flow, case-anchor, challenge and rare-safety.",
            "context_version": "gs-labs-context-truth-v1.0.0",
            "freeze_state": "frozen_before_v2_evaluated_runs",
            "split_policy": "Complete event and NPA trajectories never cross development/validation/holdout; RAT remains isolated.",
        }
    )
    (V2 / "contract.json").write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    hypothesis_coverage = json.loads(
        (V2 / "hypothesis_coverage.json").read_text(encoding="utf-8")
    )
    hypothesis_coverage["dataset_version"] = "v2.0.0"
    hypothesis_coverage["annotation_provenance"] = {
        "status": "single_team_annotation_not_customer_gold",
        "real_flow_examples": 24,
        "second_reviewer": False,
        "allowed_claim": "diagnostic comparison of configurations",
        "forbidden_claim": "statistical representativeness or customer validation",
    }
    hypothesis_coverage["B2_hypotheses"]["A1"]["inputs"] = (
        "Все B2-F в v2, включая 24 real-flow материала и challenge/rare-safety слои."
    )
    hypothesis_coverage["B2_hypotheses"]["A3"]["inputs"] = [
        row["case_id"] for row in event_cases
    ]
    hypothesis_coverage["B2_hypotheses"]["A3"]["remaining_limit"] = (
        "14 диагностических кейсов, включая real-flow; это не промышленная статистика."
    )
    (V2 / "hypothesis_coverage.json").write_text(
        json.dumps(hypothesis_coverage, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    checksums = []
    for path in sorted(V2.iterdir()):
        if path.is_file() and path.name != "checksums.sha256":
            checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    (V2 / "checksums.sha256").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    print(json.dumps(counts, ensure_ascii=False, indent=2, default=dict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
