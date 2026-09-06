#!/usr/bin/env python3
"""Freeze A2 cases from the real B1 collection evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
B1 = ROOT / "benchmarks/b1_proof/runs/20260906T015536Z_registered-disabled_1/documents.jsonl"
OUT = ROOT / "benchmarks/b3_e2e/data/a2_v1"


def load_documents() -> dict[int, dict]:
    return {
        int(row["id"]): row
        for line in B1.read_text(encoding="utf-8").splitlines()
        if (row := json.loads(line))
    }


def signal(case_id: str, row: dict, **overrides) -> dict:
    text = row["text"]
    quote = overrides.pop("quote")
    assert quote in text
    result = {
        "signal_id": case_id,
        "material_id": f"b1:{row['id']}",
        "summary": row["title"],
        "claims": [{"text": overrides.pop("claim"), "evidence_quote": quote}],
        "relevance": "relevant",
        "importance": "high",
        "interest": "GR",
        "impact": overrides.pop("impact"),
        "urgency": "planned",
        "confidence": 0.55,
        "unknowns": overrides.pop("unknowns"),
        "research_questions": overrides.pop("research_questions"),
        "kind": overrides.pop("kind", "news"),
        "recipient_roles": ["GR", "HEAD"],
        "source_title": row["title"],
    }
    result.update(overrides)
    return result


def main() -> int:
    docs = load_documents()
    hubs, reform = docs[10], docs[11]
    cases = [
        {
            "id": "A2-news",
            "description": "Market news: distinguish a discussed initiative from an adopted programme.",
            "expected_gate": "research",
            "source": hubs,
            "signal": signal(
                "A2-news",
                hubs,
                claim="Минцифры рассматривает предложения по межрегиональным хабам ЦОД.",
                quote="В Минцифры сообщили, что ведомство совместно с регионами и участниками рынка рассматривает различные предложения по развитию инфраструктуры ЦОД.",
                impact="Потенциальное изменение инфраструктуры рынка ЦОД; официальный статус и параметры пока не установлены.",
                unknowns=[
                    "Есть ли утверждённая государственная программа или пока обсуждаются предложения?",
                    "Существуют ли официальные сроки, регионы и требования к участникам?",
                ],
                research_questions=[
                    "Каков официальный статус инициативы межрегиональных хабов ЦОД?",
                    "Есть ли первичный документ со сроками, регионами и требованиями?",
                ],
            ),
        },
        {
            "id": "A2-NPA",
            "description": "NPA candidate: press risk estimate must not be confused with an enacted obligation.",
            "expected_gate": "research",
            "source": reform,
            "signal": signal(
                "A2-NPA",
                reform,
                claim="Отраслевые объединения предупреждают о рисках реформы лицензирования связи для малых операторов.",
                quote="ужесточение правил работы на рынке связи и уход малых операторов способно оставить без малого телеком-бизнеса такие регионы",
                impact="Возможное изменение условий работы операторов связи; вывод о действующих обязанностях делать нельзя без первичного документа.",
                unknowns=[
                    "Какой официальный проект лежит в основе обсуждаемой реформы?",
                    "Какова стадия проекта и какие требования в нём закреплены буквально?",
                ],
                research_questions=[
                    "Найти официальный проект реформы лицензирования операторов связи и его текущую стадию.",
                    "Какие обязанности и даты прямо следуют из официального текста?",
                ],
                kind="npa_candidate",
            ),
        },
        {
            "id": "A2-skip",
            "description": "A low-importance complete item must not consume research calls.",
            "expected_gate": "skip",
            "source": hubs,
            "signal": signal(
                "A2-skip",
                hubs,
                claim="Эксперт оценил стоимость строительства инфраструктуры ЦОД.",
                quote="строительство здания и инженерной инфраструктуры ЦОДа уровня Tier III сейчас обходится в 0,9–1,4 млрд руб. на 1 МВт IT-мощности",
                impact="Фоновая отраслевая оценка без отдельного действия.",
                unknowns=[],
                research_questions=[],
                importance="low",
                interest="IRRELEVANT",
                recipient_roles=[],
            ),
        },
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(cases, ensure_ascii=False, indent=2).encode()
    (OUT / "cases.json").write_bytes(encoded + b"\n")
    manifest = {
        "version": "a2-v1",
        "source": str(B1.relative_to(ROOT)),
        "source_sha256": hashlib.sha256(B1.read_bytes()).hexdigest(),
        "cases_sha256": hashlib.sha256(encoded + b"\n").hexdigest(),
        "case_count": len(cases),
        "truth_boundary": "Original B1 material is evidence for the starting signal; Tavily results are retained separately and audited after the live run.",
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
