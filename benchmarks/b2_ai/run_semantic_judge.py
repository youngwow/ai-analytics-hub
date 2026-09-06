#!/usr/bin/env python3
"""Run a disclosed same-model semantic judge over a frozen B2 judge packet."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(PROJECT_ROOT))

from src.common import load_env_secret  # noqa: E402
from src.config import Config  # noqa: E402
from src.paths import DEFAULT_PATHS  # noqa: E402
from src.processing.llm import build_llm_provider  # noqa: E402

MATERIAL_SCHEMA = {
    "type": "object",
    "required": ["material_results"],
    "properties": {
        "material_results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "must_fact_coverage",
                    "unsupported_claims",
                    "summary_faithfulness",
                    "impact_justification",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "must_fact_coverage": {"type": "number", "minimum": 0, "maximum": 1},
                    "unsupported_claims": {"type": "array", "items": {"type": "string"}},
                    "summary_faithfulness": {"enum": ["pass", "fail"]},
                    "impact_justification": {
                        "enum": ["pass", "fail", "not_applicable"]
                    },
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}
EVENT_SCHEMA = {
    "type": "object",
    "required": ["event_results"],
    "properties": {
        "event_results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "case_id",
                    "preserved_unique_facts",
                    "preserved_independent_positions",
                ],
                "properties": {
                    "case_id": {"type": "string"},
                    "preserved_unique_facts": {"enum": ["pass", "fail"]},
                    "preserved_independent_positions": {
                        "enum": ["pass", "fail", "not_applicable"]
                    },
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}
MATERIAL_SYSTEM = """Ты оцениваешь карточки, а не улучшаешь их. Верни только JSON и ровно все переданные id.
Корень ответа ровно такой: {"material_results":[{"id":"...","must_fact_coverage":1.0,"unsupported_claims":[],"summary_faithfulness":"pass","impact_justification":"pass"}]}.
must_fact_coverage = доля must_facts, смысл которых сохранён в summary или claims.
unsupported_claims = утверждения prediction, которых нет в material; контекст компании не является источником внешнего факта.
summary_faithfulness=pass, только если summary не искажает material.
impact_justification=pass, если impact осторожно следует из material и контекста; not_applicable — если impact пуст и материал нерелевантен.
Не снижай оценку за иную формулировку, если смысл сохранён."""
EVENT_SYSTEM = """Ты оцениваешь группировку событий. Верни только JSON и ровно все case_id.
Корень ответа ровно такой: {"event_results":[{"case_id":"...","preserved_unique_facts":"pass","preserved_independent_positions":"not_applicable"}]}.
preserved_unique_facts=pass, если все уникальные факты остались в карточках членов кластера.
preserved_independent_positions=pass, если несовместимые методики/позиции не выданы за один факт; not_applicable, если таких позиций нет.
Не придумывай отсутствующий общий summary: оценивай материалы, predictions и трассируемость."""


def chunks(rows: list[dict], size: int):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def exact_batch(provider, *, prompt, schema, system, root, id_field, expected_ids, retries):
    attempts = []
    for _ in range(retries + 1):
        data = provider.complete(prompt, schema, system=system).data
        attempts.append(data)
        rows = data.get(root)
        if isinstance(rows, list):
            actual_ids = [row.get(id_field) for row in rows if isinstance(row, dict)]
            if len(actual_ids) == len(rows) and set(actual_ids) == set(expected_ids):
                return rows, attempts
    return None, attempts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=7)
    args = parser.parse_args()
    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    config = Config.load()
    provider = build_llm_provider(
        config.llm, load_env_secret(config.llm.api_key_env, DEFAULT_PATHS.env_path)
    )
    material_results = []
    event_results = []
    raw_outputs = []
    raw_path = args.output.with_suffix(".raw.json")

    def checkpoint() -> None:
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(
            json.dumps(raw_outputs, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    try:
        for batch in chunks(packet["materials"], args.batch_size):
            expected_ids = [row["material"]["id"] for row in batch]
            rows, attempts = exact_batch(
                provider,
                prompt=json.dumps(
                    {"expected_ids": expected_ids, "items": batch}, ensure_ascii=False
                ),
                schema=MATERIAL_SCHEMA,
                system=MATERIAL_SYSTEM,
                root="material_results",
                id_field="id",
                expected_ids=expected_ids,
                retries=config.llm.max_retries,
            )
            raw_outputs.append({"type": "materials", "ids": expected_ids, "attempts": attempts})
            checkpoint()
            if rows is None:
                raise ValueError(f"judge violated material coverage for {expected_ids}")
            material_results.extend(rows)
        if packet["event_cases"]:
            expected_ids = [row["truth"]["case_id"] for row in packet["event_cases"]]
            rows, attempts = exact_batch(
                provider,
                prompt=json.dumps(
                    {"expected_ids": expected_ids, "cases": packet["event_cases"]},
                    ensure_ascii=False,
                ),
                schema=EVENT_SCHEMA,
                system=EVENT_SYSTEM,
                root="event_results",
                id_field="case_id",
                expected_ids=expected_ids,
                retries=config.llm.max_retries,
            )
            raw_outputs.append({"type": "events", "ids": expected_ids, "attempts": attempts})
            checkpoint()
            if rows is None:
                raise ValueError(f"judge violated event coverage for {expected_ids}")
            event_results.extend(rows)
    finally:
        provider.close()
    response = {
        "run_id": packet["run_id"],
        "configuration_id": packet["configuration_id"],
        "material_results": material_results,
        "event_results": event_results,
        "limitations": [
            "Judge и тестируемая система используют одну GLM-5.3; это воспроизводимая автооценка, а не независимая человеческая проверка."
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint()
    args.output.write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
