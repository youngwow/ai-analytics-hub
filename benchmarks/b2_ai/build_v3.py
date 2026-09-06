#!/usr/bin/env python3
"""Build field-adjudicated B2 v3 without inventing a new holdout."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
V2 = HERE / "data" / "v2"
V3 = HERE / "data" / "v3"
FIELDS = ("relevance", "importance", "roles", "critical_or_escalate")


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def labels(path: Path) -> dict[str, dict]:
    return {row["id"]: row for row in json.loads(path.read_text(encoding="utf-8"))["labels"]}


def normalized(field: str, value):
    return tuple(sorted(value)) if field == "roles" else value


def main() -> int:
    if V3.exists():
        shutil.rmtree(V3)
    shutil.copytree(V2, V3)
    gpt = labels(ROOT / "artifacts/b2/gold_audit_all_gpt-oss.json")
    deepseek: dict[str, dict] = {}
    for split in ("development", "validation", "holdout", "rat"):
        name = "dev" if split == "development" else split
        deepseek.update(
            labels(ROOT / f"artifacts/b2/gold_audit_{name}_deepseek-v4-flash.json")
        )

    truth = load_jsonl(V3 / "ground_truth.jsonl")
    unresolved = Counter()
    support = Counter()
    for row in truth:
        item_id = row["id"]
        scored_fields = []
        votes = {}
        for field in FIELDS:
            raw_votes = [row[field], gpt[item_id][field], deepseek[item_id][field]]
            normalized_votes = [normalized(field, value) for value in raw_votes]
            counts = Counter(normalized_votes)
            winner, count = counts.most_common(1)[0]
            votes[field] = {
                "team_v2": raw_votes[0],
                "gpt_oss_blind": raw_votes[1],
                "deepseek_blind": raw_votes[2],
            }
            if count >= 2:
                row[field] = list(winner) if field == "roles" else winner
                scored_fields.append(field)
                support[field] += 1
            else:
                unresolved[field] += 1
        row["scored_fields"] = scored_fields
        row["label_adjudication"] = {
            "method": "field_level_majority_of_team_v2_and_two_blind_llm_reviews",
            "votes": votes,
        }
    write_jsonl(V3 / "ground_truth.jsonl", truth)

    catalog = load_jsonl(V3 / "catalog.jsonl")
    for row in catalog:
        if row["split"] in {"validation", "holdout"}:
            row["split"] = "validation"
    write_jsonl(V3 / "catalog.jsonl", catalog)
    for filename in ("event_cases.jsonl", "npa_trajectories.jsonl"):
        rows = load_jsonl(V3 / filename)
        for row in rows:
            if row.get("split") in {"validation", "holdout"}:
                row["split"] = "validation"
        write_jsonl(V3 / filename, rows)

    contract = json.loads((V3 / "contract.json").read_text(encoding="utf-8"))
    contract.update(
        {
            "dataset_version": "v3.0.0",
            "created_at": "2026-09-06T16:45:00+03:00",
            "freeze_state": "gold_audited_development_and_validation_no_new_holdout_yet",
            "split_policy": "Former v2 validation/holdout are disclosed validation; holdout is intentionally empty until prompt freeze.",
            "label_policy": "Field-level majority; unresolved fields are not scored.",
        }
    )
    (V3 / "contract.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    coverage = json.loads((V3 / "coverage.json").read_text(encoding="utf-8"))
    coverage["by_split"] = dict(Counter(row["split"] for row in catalog))
    coverage["scored_field_support"] = dict(support)
    coverage["unresolved_field_counts"] = dict(unresolved)
    (V3 / "coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    hypothesis = json.loads((V3 / "hypothesis_coverage.json").read_text(encoding="utf-8"))
    hypothesis["dataset_version"] = "v3.0.0"
    hypothesis["annotation_provenance"] = {
        "status": "field_majority_internal_not_customer_gold",
        "scored_support": dict(support),
        "unresolved": dict(unresolved),
    }
    (V3 / "hypothesis_coverage.json").write_text(json.dumps(hypothesis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    checksums = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in sorted(V3.iterdir())
        if path.is_file() and path.name != "checksums.sha256"
    ]
    (V3 / "checksums.sha256").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    print(json.dumps({"dataset_version": "v3.0.0", "support": support, "unresolved": unresolved}, ensure_ascii=False, indent=2, default=dict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
