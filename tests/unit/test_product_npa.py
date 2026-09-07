from __future__ import annotations

import json
from dataclasses import replace

from support import FakeLLM

from src.processing.llm import LlmTemporaryError
from src.product.contracts import EvidenceClaim, PreparedDocument, SignalDraft
from src.product.npa import NpaResolver


def signal(signal_id="m1:s1", material_id="m1", identifier="RU-1"):
    return SignalDraft(
        signal_id=signal_id,
        material_id=material_id,
        summary="Опубликована новая редакция",
        claims=(EvidenceClaim("Редакция опубликована", "Опубликована новая редакция"),),
        relevance="relevant",
        importance="high",
        interest="GR",
        impact="Нужно проверить применимость",
        urgency="routine",
        confidence=0.8,
        kind="npa",
        npa_identifier=identifier,
        npa_stage="adopted",
        npa_version="system_software_12_months",
        npa_effective_from="2026-12-01",
        npa_change_summary="Срок сокращён",
        recipient_roles=("GR", "HEAD"),
    )


def test_npa_resolver_preserves_tracked_identity_and_valid_members():
    answer = {
        "objects": [
            {
                "object_id": "TRACKED-1",
                "member_signal_ids": ["m1:s1", "unknown"],
                "external_id": "RU-1",
                "current_stage": "adopted",
                "current_version": "v2",
                "change_summary": "Срок изменён",
                "effective_from": "2026-12-01",
                "stale_signal_ids": [],
                "needs_human_review": False,
            }
        ]
    }
    resolver = NpaResolver(FakeLLM(answer), model="glm-5.3-flash:cloud")
    result = resolver.resolve(
        [signal()],
        {"m1": PreparedDocument("m1", "НПА", "Опубликована новая редакция")},
        [
            {
                "object_id": "TRACKED-1",
                "external_id": "RU-1",
                "history": [{"event_id": "old-v1"}],
            }
        ],
    )

    assert result[0].object_id == "TRACKED-1"
    assert result[0].member_signal_ids == ("m1:s1",)
    assert result[0].current_version == "v2"
    assert result[0].prior_history_ids == ("old-v1",)


def test_npa_resolver_receives_structured_state_from_primary_analysis():
    answer = {
        "objects": [
            {
                "object_id": "NEW:RU-1",
                "member_signal_ids": ["m1:s1"],
                "external_id": "RU-1",
                "current_stage": "adopted",
                "current_version": "v1_system_software_12_months",
                "change_summary": "Срок сокращён",
                "effective_from": "2026-12-01",
                "stale_signal_ids": [],
                "needs_human_review": False,
            }
        ]
    }
    provider = FakeLLM(answer)

    NpaResolver(provider, model="glm-5.3-flash:cloud").resolve(
        [signal()],
        {"m1": PreparedDocument("m1", "НПА", "Опубликована новая редакция")},
        [],
    )

    payload = json.loads(provider.prompts[0])
    row = payload["signals"][0]
    assert row["version"] == "system_software_12_months"
    assert row["effective_from"] == "2026-12-01"
    assert row["change_summary"] == "Срок сокращён"


def test_npa_resolver_cannot_drop_unanimous_identifier_from_primary_signal():
    answer = {
        "objects": [
            {
                "object_id": "NEW:UNKNOWN:model-output",
                "member_signal_ids": ["m1:s1"],
                "external_id": None,
                "current_stage": "unknown",
                "current_version": "unknown",
                "change_summary": "unknown",
                "effective_from": None,
                "stale_signal_ids": [],
                "needs_human_review": True,
            }
        ]
    }
    result = NpaResolver(FakeLLM(answer), model="glm-5.3-flash:cloud").resolve(
        [signal(identifier="Постановление № 1120")],
        {
            "m1": PreparedDocument(
                "m1",
                "НПА",
                "Постановление № 1120",
                source_class="regulator",
            )
        },
        [],
    )

    assert result[0].external_id == "Постановление № 1120"
    assert result[0].object_id == "NEW:Постановление № 1120"


def test_large_npa_bank_uses_embedding_candidates_and_keeps_exact_id():
    answer = {
        "objects": [
            {
                "object_id": "TRACKED-24",
                "member_signal_ids": ["m1:s1"],
                "external_id": "RU-1",
                "current_stage": "adopted",
                "current_version": "v2",
                "change_summary": "Срок изменён",
                "effective_from": "2026-12-01",
                "stale_signal_ids": [],
                "needs_human_review": False,
            }
        ]
    }

    def vectors(texts):
        return [[1.0, 0.0] if "RU-1" in text else [0.0, 1.0] for text in texts]

    provider = FakeLLM(answer, embedder=vectors)
    tracked = [
        {
            "object_id": f"TRACKED-{index}",
            "external_id": "RU-1" if index == 24 else f"RU-{100 + index}",
            "title": f"Проект {index}",
            "current_version": "v1",
        }
        for index in range(25)
    ]

    result = NpaResolver(provider, model="glm-5.3-flash:cloud", embedder=provider).resolve(
        [signal()],
        {"m1": PreparedDocument("m1", "НПА", "Опубликована новая редакция")},
        tracked,
        mode="adaptive",
    )

    payload = json.loads(provider.prompts[0])
    assert len(payload["tracked_npas"]) <= 20
    assert any(row["object_id"] == "TRACKED-24" for row in payload["tracked_npas"])
    assert result[0].object_id == "TRACKED-24"


def test_large_npa_bank_without_embedder_degrades_to_exact_ids_only():
    answer = {
        "objects": [
            {
                "object_id": "TRACKED-24",
                "member_signal_ids": ["m1:s1"],
                "external_id": "RU-1",
                "current_stage": "adopted",
                "current_version": "v2",
                "change_summary": "Срок изменён",
                "effective_from": "2026-12-01",
                "stale_signal_ids": [],
                "needs_human_review": False,
            }
        ]
    }
    provider = FakeLLM(answer)
    tracked = [
        {
            "object_id": f"TRACKED-{index}",
            "external_id": "RU-1" if index == 24 else f"RU-{100 + index}",
        }
        for index in range(25)
    ]

    NpaResolver(provider, model="glm-5.3-flash:cloud").resolve(
        [signal()],
        {"m1": PreparedDocument("m1", "НПА", "Опубликована новая редакция")},
        tracked,
        mode="adaptive",
    )

    payload = json.loads(provider.prompts[0])
    assert [row["object_id"] for row in payload["tracked_npas"]] == ["TRACKED-24"]


def test_npa_resolver_fails_safe_without_dropping_signal():
    resolver = NpaResolver(FakeLLM(LlmTemporaryError("down")), model="glm-5.3-flash:cloud")
    result = resolver.resolve(
        [signal(identifier=None)],
        {"m1": PreparedDocument("m1", "НПА", "Опубликована новая редакция")},
        [],
    )

    assert result[0].member_signal_ids == ("m1:s1",)
    assert result[0].effective_from == "2026-12-01"
    assert result[0].needs_human_review is True


def test_npa_fallback_groups_same_confirmed_identifier():
    resolver = NpaResolver(FakeLLM(LlmTemporaryError("down")), model="glm-5.3-flash:cloud")
    signals = [
        signal("official:s1", "official", "RU-1"),
        signal("secondary:s1", "secondary", "RU-1"),
    ]
    result = resolver.resolve(
        signals,
        {
            "official": PreparedDocument(
                "official", "НПА", "Опубликована новая редакция", source_class="regulator"
            ),
            "secondary": PreparedDocument("secondary", "Обзор", "Опубликована новая редакция"),
        },
        [],
    )

    assert len(result) == 1
    assert result[0].object_id == "NEW:RU-1"
    assert result[0].member_signal_ids == ("official:s1", "secondary:s1")
    assert result[0].stale_signal_ids == ("secondary:s1",)
    assert result[0].current_version == "v1_system_software_12_months"
    assert result[0].effective_from == "2026-12-01"


def test_official_old_state_stays_in_history_while_secondary_is_stale():
    answer = {
        "objects": [
            {
                "object_id": "TRACKED-1",
                "member_signal_ids": ["old:s1", "current:s1", "repost:s1"],
                "external_id": "RU-1",
                "current_stage": "adopted",
                "current_version": "v3",
                "change_summary": "changed",
                "effective_from": "2026-12-01",
                "stale_signal_ids": ["old:s1", "repost:s1"],
                "needs_human_review": False,
            }
        ]
    }
    signals = [
        signal("old:s1", "old"),
        signal("current:s1", "current"),
        signal("repost:s1", "repost"),
    ]
    documents = {
        "old": PreparedDocument(
            "old", "Старая стадия", "Опубликована новая редакция", source_class="regulator"
        ),
        "current": PreparedDocument(
            "current", "Акт принят", "Опубликована новая редакция", source_class="regulator"
        ),
        "repost": PreparedDocument("repost", "Пересказ", "Опубликована новая редакция"),
    }

    result = NpaResolver(FakeLLM(answer), model="glm-5.3-flash:cloud").resolve(
        signals, documents, [{"object_id": "TRACKED-1", "external_id": "RU-1"}]
    )

    assert result[0].stale_signal_ids == ("repost:s1",)


def test_latest_official_signal_controls_current_stage():
    answer = {
        "objects": [
            {
                "object_id": "TRACKED-1",
                "member_signal_ids": ["old:s1", "current:s1"],
                "external_id": "RU-1",
                "current_stage": "public_discussion",
                "current_version": "v2",
                "change_summary": "changed",
                "effective_from": None,
                "stale_signal_ids": [],
                "needs_human_review": False,
            }
        ]
    }
    old = signal("old:s1", "old")
    current = signal("current:s1", "current")
    result = NpaResolver(FakeLLM(answer), model="glm-5.3-flash:cloud").resolve(
        [old, current],
        {
            "old": PreparedDocument(
                "old", "Проект", "Текст", published_at="2026-08-01", source_class="regulator"
            ),
            "current": PreparedDocument(
                "current",
                "Акт принят",
                "Текст",
                published_at="2026-09-01",
                source_class="regulator",
            ),
        },
        [{"object_id": "TRACKED-1", "external_id": "RU-1"}],
    )

    assert result[0].current_stage == "adopted"


def test_revised_draft_advances_new_internal_version_without_effective_date():
    answer = {
        "objects": [
            {
                "object_id": "NEW:RU-1",
                "member_signal_ids": ["first:s1", "revised:s1"],
                "external_id": "RU-1",
                "current_stage": "public_discussion",
                "current_version": "v1_12_months",
                "change_summary": "Добавлен срок 12 месяцев",
                "effective_from": "2027-09-01",
                "stale_signal_ids": [],
                "needs_human_review": False,
            }
        ]
    }
    first = replace(signal("first:s1", "first"), npa_stage="public_discussion")
    revised = replace(
        signal("revised:s1", "revised"),
        npa_stage="опубликована доработанная редакция проекта",
        npa_change_summary="Добавлен срок 12 месяцев",
        npa_effective_from="2027-09-01",
    )
    result = NpaResolver(FakeLLM(answer), model="glm-5.3-flash:cloud").resolve(
        [first, revised],
        {
            "first": PreparedDocument(
                "first", "Проект", "Текст", published_at="2026-08-01", source_class="regulator"
            ),
            "revised": PreparedDocument(
                "revised",
                "Новая редакция",
                "Текст",
                published_at="2026-09-01",
                source_class="regulator",
            ),
        },
        [],
    )

    assert result[0].current_stage == "revised_draft"
    assert result[0].current_version == "v2_12_months"
    assert result[0].effective_from is None


def test_unidentified_candidate_can_attach_to_confirmed_npa():
    grouped = {
        "objects": [
            {
                "object_id": "NEW:RU-1",
                "member_signal_ids": ["official:s1"],
                "external_id": "RU-1",
                "current_stage": "draft",
                "current_version": "v1",
                "change_summary": "new",
                "effective_from": None,
                "stale_signal_ids": [],
                "needs_human_review": False,
            },
            {
                "object_id": "NEW:UNKNOWN:x",
                "member_signal_ids": ["repost:s1"],
                "external_id": None,
                "current_stage": "unknown",
                "current_version": "unknown",
                "change_summary": "unknown",
                "effective_from": None,
                "stale_signal_ids": [],
                "needs_human_review": True,
            },
        ]
    }
    attach = {
        "object_id": "NEW:RU-1",
        "relation": "same_npa",
        "confidence": 0.9,
        "reason": "same document",
    }
    resolver = NpaResolver(FakeLLM([grouped, attach]), model="glm-5.3-flash:cloud")
    signals = [
        signal("official:s1", "official", "RU-1"),
        signal("repost:s1", "repost", None),
    ]
    result = resolver.resolve(
        signals,
        {
            "official": PreparedDocument(
                "official",
                "Проект RU-1",
                "Опубликована новая редакция",
                source_class="regulator",
            ),
            "repost": PreparedDocument(
                "repost", "Правило действует", "Опубликована новая редакция"
            ),
        },
        [],
    )

    assert len(result) == 1
    assert result[0].member_signal_ids == ("official:s1", "repost:s1")
    assert result[0].stale_signal_ids == ("repost:s1",)
