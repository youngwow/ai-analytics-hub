#!/usr/bin/env python3
"""Finalize the post-freeze B2 v4 holdout from two blind agreements.

Only fields on which both independent judges agree are scored. Disagreements
remain visible in the adjudication trace and are excluded from metrics.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(__file__).resolve().parent / "data" / "v4"
ARTIFACTS = ROOT / "artifacts" / "b2"
FIELDS = ("relevance", "importance", "roles", "critical_or_escalate")


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def load_labels(path: Path) -> tuple[str, dict[str, dict]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["judge_model"], {row["id"]: row for row in payload["labels"]}


def normalized(field: str, value):
    return tuple(sorted(value)) if field == "roles" else value


def main() -> int:
    deepseek_model, deepseek = load_labels(
        ARTIFACTS / "gold_audit_v4_holdout_deepseek-v4-flash.json"
    )
    gpt_model, gpt = load_labels(ARTIFACTS / "gold_audit_v4_holdout_gpt-oss.json")
    catalog = {row["id"]: row for row in load_jsonl(DATA / "catalog.jsonl")}
    holdout_ids = {item_id for item_id, row in catalog.items() if row["split"] == "holdout"}
    if set(deepseek) != holdout_ids or set(gpt) != holdout_ids:
        raise ValueError("Blind audit coverage does not exactly match frozen holdout")

    support: Counter[str] = Counter()
    disagreements: Counter[str] = Counter()
    truth = load_jsonl(DATA / "ground_truth.jsonl")
    for row in truth:
        item_id = row["id"]
        if item_id not in holdout_ids:
            continue
        scored_fields: list[str] = []
        votes: dict[str, dict] = {}
        for field in FIELDS:
            left = deepseek[item_id][field]
            right = gpt[item_id][field]
            votes[field] = {deepseek_model: left, gpt_model: right}
            if normalized(field, left) == normalized(field, right):
                row[field] = sorted(left) if field == "roles" else left
                scored_fields.append(field)
                support[field] += 1
            else:
                disagreements[field] += 1
        row["scored_fields"] = scored_fields
        row["label_adjudication"] = {
            "method": "agreement_of_two_blind_llm_reviews_no_team_tiebreak",
            "votes": votes,
        }
    write_jsonl(DATA / "ground_truth.jsonl", truth)

    contract = json.loads((DATA / "contract.json").read_text(encoding="utf-8"))
    contract.update(
        {
            "dataset_version": "v4.0.0",
            "freeze_state": "post_prompt_freeze_holdout_final",
            "split_policy": "Development and validation inherit audited v3; 27-item holdout was created only after prompt/config freeze and never used for tuning.",
            "label_policy": "Existing v3 fields use field majority. Fresh holdout fields are scored only on agreement of two blind judges; disagreements are excluded.",
        }
    )
    (DATA / "contract.json").write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    coverage = json.loads((DATA / "coverage.json").read_text(encoding="utf-8"))
    all_support: Counter[str] = Counter()
    scored_distributions = {
        "relevance": Counter(),
        "importance": Counter(),
        "roles": Counter(),
        "critical_or_escalate": Counter(),
    }
    for row in truth:
        for field in row.get("scored_fields", FIELDS):
            if field not in FIELDS:
                continue
            all_support[field] += 1
            if field == "roles":
                for role in row[field]:
                    scored_distributions[field][role] += 1
            else:
                scored_distributions[field][str(row[field]).lower()] += 1
    coverage["scored_field_support"] = dict(all_support)
    coverage["scored_label_distributions"] = {
        field: dict(values) for field, values in scored_distributions.items()
    }
    coverage["fresh_holdout"].update(
        {
            "scored_field_support": dict(support),
            "disagreement_counts": dict(disagreements),
            "label_source": "agreement of two blind LLM judges; not customer gold",
        }
    )
    (DATA / "coverage.json").write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    hypothesis = json.loads((DATA / "hypothesis_coverage.json").read_text(encoding="utf-8"))
    hypothesis["dataset_version"] = "v4.0.0"
    hypothesis["annotation_provenance"] = {
        "status": "mixed_internal_gold_not_customer_gold",
        "existing_v3": "field majority of team label and two blind LLM reviews",
        "fresh_holdout": "agreement of two blind LLM reviews with disagreements excluded",
        "scored_support": dict(all_support),
    }
    (DATA / "hypothesis_coverage.json").write_text(
        json.dumps(hypothesis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    checksums = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in sorted(DATA.iterdir())
        if path.is_file() and path.name != "checksums.sha256"
    ]
    (DATA / "checksums.sha256").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    print(json.dumps({"support": support, "disagreements": disagreements}, ensure_ascii=False, indent=2, default=dict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
