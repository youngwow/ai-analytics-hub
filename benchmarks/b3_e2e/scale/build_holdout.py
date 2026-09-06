#!/usr/bin/env python3
"""Freeze a post-fix B3-SCALE holdout without changing the measured v1 bank."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .build_scale import DEFAULT_OUTPUT

OVERLAY = (
    {
        "position": 1_500,
        "id": "H-EVT-AURORA-VOICE",
        "kind": "event",
        "title": "Оператор Аврора начал пилот голосового управления телевидением",
        "text": "Аврора запустила в Казани пилот голосового управления телеканалами для части абонентов.",
    },
    {
        "position": 1_501,
        "id": "H-EVT-AURORA-BRAND",
        "kind": "event",
        "title": "Оператор Аврора обновил федеральную рекламную кампанию",
        "text": "Аврора представила новые рекламные ролики бренда. Кампания не связана с пилотом голосового управления.",
    },
    {
        "position": 2_750,
        "id": "H-EVT-DVB-WORKGROUP",
        "kind": "event",
        "title": "Рабочая группа начала разработку рекомендаций по гибридному вещанию",
        "text": "Вещатели и разработчики создали рабочую группу для рекомендаций по единому стандарту гибридного вещания.",
    },
    {
        "position": 2_751,
        "id": "H-EVT-DVB-FORUM",
        "kind": "event",
        "title": "Организаторы анонсировали независимый форум по гибридному вещанию",
        "text": "Коммерческий форум посвящён гибридному вещанию, но не связан с отраслевой рабочей группой.",
    },
    {
        "position": 6_403,
        "id": "H-NPA-SMARTTV-DATA",
        "kind": "npa",
        "external_id": "REG-TV-DATA-204",
        "title": "Правила хранения телеметрии Smart TV",
        "text": "Проект REG-TV-DATA-204 устанавливает срок хранения обезличенной телеметрии Smart TV 18 месяцев.",
    },
    {
        "position": 6_404,
        "id": "H-NPA-MED-DATA",
        "kind": "npa",
        "external_id": "REG-MED-DATA-204",
        "title": "Правила хранения медицинской телеметрии",
        "text": "Проект REG-MED-DATA-204 относится к медицинским устройствам и является отдельным документом.",
    },
    {
        "position": 8_503,
        "id": "H-NPA-AD-TRANSPARENCY",
        "kind": "npa",
        "external_id": "REG-AD-TV-911",
        "title": "Правила прозрачности адресной рекламы на телевидении",
        "text": "Документ REG-AD-TV-911 принят в первой редакции и регулирует раскрытие параметров адресной телерекламы.",
    },
    {
        "position": 8_504,
        "id": "H-NPA-AD-WEB",
        "kind": "npa",
        "external_id": "REG-AD-WEB-911",
        "title": "Правила прозрачности рекламы в интернете",
        "text": "REG-AD-WEB-911 регулирует интернет-рекламу и не является редакцией документа о телевидении.",
    },
)

CASES = (
    {
        "id": "H_E_voice_update", "min_size": 10_000, "kind": "event",
        "expected_object_id": "H-EVT-AURORA-VOICE", "expected_relation": "event_update",
        "text": "Аврора расширила казанский пилот голосового переключения телеканалов на всех абонентов региона.",
    },
    {
        "id": "H_E_voice_same_actor_negative", "min_size": 10_000, "kind": "event",
        "expected_object_id": None, "expected_relation": "different",
        "text": "Аврора начала отдельную программу лояльности для покупателей домашнего интернета.",
    },
    {
        "id": "H_E_workgroup_update", "min_size": 10_000, "kind": "event",
        "expected_object_id": "H-EVT-DVB-WORKGROUP", "expected_relation": "event_update",
        "text": "Созданная вещателями рабочая группа завершила первую редакцию рекомендаций по гибридному вещанию.",
    },
    {
        "id": "H_E_forum_negative", "min_size": 10_000, "kind": "event",
        "expected_object_id": None, "expected_relation": "different",
        "text": "Другая команда объявила новый коммерческий форум о гибридном ТВ; к рабочей группе он не относится.",
    },
    {
        "id": "H_N_smarttv_update", "min_size": 10_000, "kind": "npa",
        "expected_object_id": "H-NPA-SMARTTV-DATA", "expected_relation": "same_npa",
        "external_id": "REG-TV-DATA-204",
        "text": "В доработанном REG-TV-DATA-204 срок хранения телеметрии Smart TV сокращён до 12 месяцев.",
    },
    {
        "id": "H_N_data_negative", "min_size": 10_000, "kind": "npa",
        "expected_object_id": None, "expected_relation": "different",
        "external_id": "REG-IOT-DATA-205",
        "text": "Опубликован новый самостоятельный REG-IOT-DATA-205 о телеметрии промышленных датчиков.",
    },
    {
        "id": "H_N_ad_update", "min_size": 10_000, "kind": "npa",
        "expected_object_id": "H-NPA-AD-TRANSPARENCY", "expected_relation": "same_npa",
        "external_id": "REG-AD-TV-911",
        "text": "Вторая редакция REG-AD-TV-911 добавила требования к отчётам по адресной телерекламе.",
    },
    {
        "id": "H_N_ad_negative", "min_size": 10_000, "kind": "npa",
        "expected_object_id": None, "expected_relation": "different",
        "external_id": "REG-AD-AUDIO-912",
        "text": "Новый REG-AD-AUDIO-912 регулирует прозрачность рекламы в аудиосервисах.",
    },
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(root: Path = DEFAULT_OUTPUT) -> None:
    overlay = root / "holdout_overlay.json"
    cases = root / "holdout_cases.json"
    overlay.write_text(json.dumps(OVERLAY, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cases.write_text(json.dumps(CASES, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "benchmark": "B3-SCALE-HOLDOUT",
        "version": "1.0.0",
        "base_bank_sha256": sha256(root / "bank.jsonl"),
        "overlay_sha256": sha256(overlay),
        "cases_sha256": sha256(cases),
        "created_before_resolver_fix": True,
        "objects_replaced": len(OVERLAY),
        "cases": len(CASES),
    }
    (root / "holdout_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    build()
