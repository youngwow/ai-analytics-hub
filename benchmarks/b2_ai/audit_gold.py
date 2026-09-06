#!/usr/bin/env python3
"""Blind second-opinion audit of B2 labels against Context Truth.

The judge never receives existing labels. Outputs are proposals for explicit
adjudication; this script never rewrites a frozen dataset.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from src.common import load_env_secret  # noqa: E402
from src.config import Config  # noqa: E402
from src.paths import DEFAULT_PATHS  # noqa: E402
from src.processing.llm import build_llm_provider  # noqa: E402

SYSTEM = """Ты проводишь слепой аудит проектной разметки для GS Labs.
Верни только JSON и ровно все переданные id. Ты НЕ видишь существующие gold-метки.
Оцени каждый материал только по его тексту и Context Truth. Не расширяй интересы компании
по одному совпадению темы. HEAD назначай лишь при управленческой значимости; PR — при
коммуникационной/репутационной задаче; GR — при регуляторном, госпроектном или policy-влиянии.
critical требует подтверждённой немедленной угрозы/обязанности, а не просто важной темы.
rationale должен назвать конкретное правило контекста и не более двух предложений.
Корень ответа ОБЯЗАТЕЛЬНО ровно такой:
{"labels":[{"id":"...","relevance":"relevant","importance":"medium",
"roles":["PR"],"critical_or_escalate":false,"rationale":"..."}]}.
Не возвращай отдельное поле ids и не опускай ни одного поля label."""
SCHEMA = {
    "type": "object",
    "required": ["labels"],
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "relevance",
                    "importance",
                    "roles",
                    "critical_or_escalate",
                    "rationale",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "relevance": {
                        "enum": ["relevant", "borderline", "irrelevant", "unknown"]
                    },
                    "importance": {"enum": ["low", "medium", "high", "critical"]},
                    "roles": {
                        "type": "array",
                        "items": {"enum": ["PR", "GR", "HEAD"]},
                    },
                    "critical_or_escalate": {"type": "boolean"},
                    "rationale": {"type": "string"},
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def chunks(rows: list[dict], size: int):
    for offset in range(0, len(rows), size):
        yield rows[offset : offset + size]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="v2")
    parser.add_argument("--split", default="development")
    parser.add_argument(
        "--model",
        choices=["deepseek-v4-flash:cloud", "gpt-oss:120b-cloud"],
        required=True,
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    data = Path(__file__).resolve().parent / "data" / args.version
    catalog = {row["id"]: row for row in load_jsonl(data / "catalog.jsonl")}
    materials = [
        row
        for row in load_jsonl(data / "materials.jsonl")
        if args.split == "all" or catalog[row["id"]]["split"] == args.split
    ]
    context = json.loads((data / "context_gs_labs.json").read_text(encoding="utf-8"))
    config = Config.load()
    llm_config = replace(config.llm, model=args.model, temperature=0.0)
    provider = build_llm_provider(
        llm_config,
        load_env_secret(llm_config.api_key_env, DEFAULT_PATHS.env_path),
    )
    partial_path = args.output.with_suffix(".partial.json")
    prior = (
        json.loads(partial_path.read_text(encoding="utf-8"))
        if args.resume and partial_path.exists()
        else {}
    )
    labels: list[dict] = list(prior.get("labels") or [])
    raw: list[dict] = list(prior.get("raw_batches") or [])
    completed_ids = {row["id"] for row in labels}
    materials = [row for row in materials if row["id"] not in completed_ids]

    def checkpoint(status: str) -> None:
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        partial_path.write_text(
            json.dumps(
                {
                    "status": status,
                    "dataset_version": args.version,
                    "split": args.split,
                    "judge_model": args.model,
                    "labels": labels,
                    "raw_batches": raw,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    try:
        for batch in chunks(materials, args.batch_size):
            expected = [row["id"] for row in batch]
            rows = None
            actual = []
            for _ in range(3):
                completion = provider.complete(
                    json.dumps(
                        {"context": context, "expected_ids": expected, "materials": batch},
                        ensure_ascii=False,
                    ),
                    SCHEMA,
                    system=SYSTEM,
                )
                candidate = completion.data.get("labels")
                actual = (
                    [row.get("id") for row in candidate]
                    if isinstance(candidate, list)
                    else []
                )
                raw.append({"expected": expected, "response": completion.data})
                if len(actual) == len(expected) and set(actual) == set(expected):
                    rows = candidate
                    break
            if rows is None:
                checkpoint("FAILED")
                raise ValueError(
                    f"judge coverage mismatch: expected={expected}, actual={actual}, "
                    f"last_response={raw[-1]['response']}"
                )
            labels.extend(rows)
            checkpoint("RUNNING")
    finally:
        provider.close()
    result = {
        "status": "PROPOSAL_ONLY",
        "dataset_version": args.version,
        "split": args.split,
        "judge_model": args.model,
        "existing_gold_was_visible": False,
        "labels": labels,
        "limitations": [
            "LLM second opinion is not customer gold.",
            "No frozen label is changed without explicit adjudication.",
        ],
        "raw_batches": raw,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    checkpoint("COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
