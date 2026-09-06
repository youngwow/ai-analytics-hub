#!/usr/bin/env python3
"""Build the deterministic B3-SCALE world.

The corpus is synthetic by design. Target stories and their hard negatives are
hand-authored; bulk distractors are deterministic combinations of domain-like
topics, actors, regions and stages. The three bank sizes are nested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = ROOT / "benchmarks" / "b3_e2e" / "data" / "scale_v1"
SIZES = (100, 1_000, 10_000)

TOPICS = (
    "интерактивное телевидение",
    "гибридное вещание",
    "адресная реклама",
    "телевизионная аналитика",
    "голосовое управление",
    "рекомендательные сервисы",
    "защита пользовательских данных",
    "доступность интерфейсов",
    "предустановка приложений",
    "региональная медиаплатформа",
    "цифровая маркировка контента",
    "измерение телеаудитории",
)
ACTORS = (
    "оператор Альфа",
    "оператор Вектор",
    "медиахолдинг Север",
    "регулятор связи",
    "отраслевая ассоциация",
    "разработчик платформы Маяк",
    "региональный вещатель",
    "производитель приставок Спектр",
)
REGIONS = ("Москва", "Санкт-Петербург", "Казань", "Пермь", "Томск", "Самара")
ACTIONS = (
    "объявил пилот",
    "опубликовал результаты исследования",
    "обновил программную платформу",
    "начал тестирование",
    "закрыл отраслевой проект",
    "представил методику",
)
NPA_TOPICS = (
    "предустановка медиаприложений",
    "обработка обезличенных телеметрических данных",
    "доступность интерфейсов Smart TV",
    "маркировка интернет-рекламы",
    "хранение данных пользователей",
    "распространение обязательных телеканалов",
)
NPA_STAGES = ("проект", "общественное обсуждение", "внесён", "принят", "вступил в силу")


SPECIAL: dict[int, dict] = {
    8: {
        "id": "EVT-STINGRAY-2026",
        "kind": "event",
        "title": "GS Labs выпустила крупное обновление платформы StingrayTV",
        "text": "GS Labs обновила StingrayTV: ускорен запуск приложений и добавлен новый интерфейс рекомендаций.",
    },
    9: {
        "id": "EVT-STINGRAY-MARKET",
        "kind": "event",
        "title": "Рынок платформ интерактивного телевидения в 2026 году",
        "text": "Обзор рынка упоминает StingrayTV среди нескольких платформ, но не сообщает о выпуске обновления.",
    },
    73: {
        "id": "NPA-PREINSTALL",
        "kind": "npa",
        "external_id": "02/15/07-26/00181111",
        "title": "Проект требований к предустановке медиаприложений",
        "text": "Документ 02/15/07-26/00181111 проходит общественное обсуждение. Редакция v1.",
    },
    74: {
        "id": "NPA-PREINSTALL-OTHER",
        "kind": "npa",
        "external_id": "02/15/07-26/00181112",
        "title": "Отдельный проект о предустановке образовательных приложений",
        "text": "Документ 02/15/07-26/00181112 регулирует образовательные приложения и не связан с медиаприложениями.",
    },
    412: {
        "id": "EVT-VECTOR-ADS",
        "kind": "event",
        "title": "Оператор Вектор начал пилот адресной рекламы",
        "text": "Вектор запустил пилот адресной рекламы в Самаре на части абонентской базы.",
    },
    413: {
        "id": "EVT-VECTOR-ADS-OTHER",
        "kind": "event",
        "title": "Вектор обновил обычную рекламную кампанию",
        "text": "Оператор Вектор сменил креативы федеральной имиджевой рекламы. Это не пилот адресной рекламы.",
    },
    887: {
        "id": "NPA-TELEMETRY",
        "kind": "npa",
        "external_id": "02/15/09-26/00990001",
        "title": "Проект правил обработки обезличенной телеметрии",
        "text": "Документ 02/15/09-26/00990001 опубликован как проект. Срок хранения — 12 месяцев.",
    },
    888: {
        "id": "NPA-TELEMETRY-OTHER",
        "kind": "npa",
        "external_id": "02/15/09-26/00990002",
        "title": "Проект правил хранения медицинской телеметрии",
        "text": "Документ 02/15/09-26/00990002 относится только к медицинским устройствам.",
    },
    4_096: {
        "id": "EVT-HYBRID-TV-ROUND-TABLE",
        "kind": "event",
        "title": "Круглый стол по стандарту гибридного телевидения",
        "text": "Ассоциация и вещатели обсудили единый стандарт гибридного телевидения и договорились подготовить рекомендации.",
    },
    4_097: {
        "id": "EVT-HYBRID-TV-CONFERENCE",
        "kind": "event",
        "title": "Независимая конференция по гибридному телевидению",
        "text": "Другая группа организаторов анонсировала конференцию. Она не связана с отраслевым круглым столом.",
    },
    9_001: {
        "id": "NPA-ACCESS-OLD",
        "kind": "npa",
        "external_id": "STD-TV-14",
        "title": "Стандарт доступности интерфейсов Smart TV",
        "text": "Стандарт STD-TV-14 принят в первой редакции. Требования носят добровольный характер.",
    },
    9_002: {
        "id": "NPA-ACCESS-SIMILAR",
        "kind": "npa",
        "external_id": "STD-MOBILE-14",
        "title": "Стандарт доступности мобильных интерфейсов",
        "text": "STD-MOBILE-14 регулирует мобильные приложения и является отдельным документом.",
    },
}

CASES = (
    {
        "id": "E100_same_event",
        "min_size": 100,
        "kind": "event",
        "expected_object_id": "EVT-STINGRAY-2026",
        "expected_relation": "same_event",
        "text": "В обновлённой StingrayTV от GS Labs появились быстрый запуск приложений и переработанные рекомендации.",
    },
    {
        "id": "E100_hard_negative",
        "min_size": 100,
        "kind": "event",
        "expected_object_id": None,
        "expected_relation": "different",
        "text": "Аналитики сравнили несколько решений рынка интерактивного ТВ, включая StingrayTV; новых релизов не объявлено.",
    },
    {
        "id": "N100_known_update",
        "min_size": 100,
        "kind": "npa",
        "expected_object_id": "NPA-PREINSTALL",
        "expected_relation": "same_npa",
        "external_id": "02/15/07-26/00181111",
        "text": "Проект 02/15/07-26/00181111 доработан после обсуждения и внесён в правительство.",
    },
    {
        "id": "N100_similar_other",
        "min_size": 100,
        "kind": "npa",
        "expected_object_id": None,
        "expected_relation": "different",
        "external_id": "02/15/07-26/00181113",
        "text": "Опубликован новый отдельный документ 02/15/07-26/00181113 о детских приложениях.",
    },
    {
        "id": "E1000_event_update",
        "min_size": 1_000,
        "kind": "event",
        "expected_object_id": "EVT-VECTOR-ADS",
        "expected_relation": "event_update",
        "text": "Вектор расширил ранее начатый в Самаре пилот адресной рекламы на всю региональную абонентскую базу.",
    },
    {
        "id": "E1000_same_actor_other_story",
        "min_size": 1_000,
        "kind": "event",
        "expected_object_id": None,
        "expected_relation": "different",
        "text": "Оператор Вектор запустил несвязанную федеральную кампанию по продвижению домашнего интернета.",
    },
    {
        "id": "N1000_known_update",
        "min_size": 1_000,
        "kind": "npa",
        "expected_object_id": "NPA-TELEMETRY",
        "expected_relation": "same_npa",
        "external_id": "02/15/09-26/00990001",
        "text": "Документ 02/15/09-26/00990001 внесён в Госдуму; срок хранения сокращён с 12 до 6 месяцев.",
    },
    {
        "id": "N1000_same_topic_other_id",
        "min_size": 1_000,
        "kind": "npa",
        "expected_object_id": None,
        "expected_relation": "different",
        "external_id": "02/15/09-26/00990003",
        "text": "Появился отдельный проект 02/15/09-26/00990003 о промышленной телеметрии.",
    },
    {
        "id": "E10000_old_event_update",
        "min_size": 10_000,
        "kind": "event",
        "expected_object_id": "EVT-HYBRID-TV-ROUND-TABLE",
        "expected_relation": "event_update",
        "text": "Участники августовского круглого стола завершили рекомендации по единому стандарту гибридного ТВ.",
    },
    {
        "id": "E10000_old_event_negative",
        "min_size": 10_000,
        "kind": "event",
        "expected_object_id": None,
        "expected_relation": "different",
        "text": "Новые организаторы открыли регистрацию на отдельную конференцию о гибридном телевидении.",
    },
    {
        "id": "N10000_old_npa_update",
        "min_size": 10_000,
        "kind": "npa",
        "expected_object_id": "NPA-ACCESS-OLD",
        "expected_relation": "same_npa",
        "external_id": "STD-TV-14",
        "text": "Опубликована вторая редакция STD-TV-14: часть требований к доступности Smart TV стала обязательной.",
    },
    {
        "id": "N10000_old_npa_negative",
        "min_size": 10_000,
        "kind": "npa",
        "expected_object_id": None,
        "expected_relation": "different",
        "external_id": "STD-WEB-14",
        "text": "Опубликован новый отдельный стандарт STD-WEB-14 для веб-интерфейсов.",
    },
)


def regular_object(index: int) -> dict:
    # About ten percent of the bank are tracked regulatory objects. This is not
    # claimed as production prevalence; it ensures both stores grow materially.
    if index % 10 == 3:
        topic = NPA_TOPICS[(index // 10) % len(NPA_TOPICS)]
        stage = NPA_STAGES[(index // 60) % len(NPA_STAGES)]
        external_id = f"REG-{2020 + index % 7}-{index:05d}"
        return {
            "id": f"NPA-{index:05d}",
            "kind": "npa",
            "external_id": external_id,
            "title": f"{stage.capitalize()}: {topic} ({external_id})",
            "text": f"{stage.capitalize()} документа {external_id} по теме «{topic}». Регион применения: {REGIONS[index % len(REGIONS)]}.",
        }
    topic = TOPICS[index % len(TOPICS)]
    actor = ACTORS[(index // len(TOPICS)) % len(ACTORS)]
    action = ACTIONS[(index // (len(TOPICS) * len(ACTORS))) % len(ACTIONS)]
    region = REGIONS[(index * 5) % len(REGIONS)]
    month = 1 + index % 12
    return {
        "id": f"EVT-{index:05d}",
        "kind": "event",
        "title": f"{actor.capitalize()} {action}: {topic}",
        "text": f"{actor.capitalize()} {action} по теме «{topic}» в регионе {region}. Период: 2026-{month:02d}.",
    }


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    bank_path = output / "bank.jsonl"
    with bank_path.open("w", encoding="utf-8") as stream:
        for index in range(max(SIZES)):
            row = SPECIAL.get(index, regular_object(index)) | {"position": index}
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    cases_path = output / "cases.json"
    write_json(cases_path, list(CASES))
    manifest = {
        "benchmark": "B3-SCALE",
        "version": "1.0.0",
        "bank_sizes": list(SIZES),
        "nested_banks": True,
        "bulk_data": "deterministic synthetic distractors",
        "targets": "hand-authored domain cases and hard negatives",
        "intended_claim": "A3 candidate retrieval and final resolution under bank growth",
        "non_claims": [
            "natural production prevalence",
            "human PR/GR value",
            "quality outside the represented cases",
        ],
        "files": {
            "bank.jsonl": sha256(bank_path),
            "cases.json": sha256(cases_path),
        },
    }
    write_json(output / "manifest.json", manifest)
    print(f"built {output}: {max(SIZES)} objects, {len(CASES)} cases")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    build(args.output)


if __name__ == "__main__":
    main()
