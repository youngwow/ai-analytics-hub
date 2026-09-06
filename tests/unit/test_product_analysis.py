from __future__ import annotations

from support import FakeLLM

from src.processing.llm import LlmTemporaryError
from src.product.analysis import PrimaryAnalyzer
from src.product.contracts import ContextBlock, GsLabsContext, PreparedDocument


def context():
    return GsLabsContext("v1", ContextBlock("GS Labs"), ContextBlock("PR"), ContextBlock("GR"))


def test_context_accepts_flat_external_profile():
    result = GsLabsContext.from_dict(
        {
            "company": "GS Labs",
            "relation": "Digital television developer",
            "interests": ["subscriber devices", "content protection"],
            "roles": {
                "PR": "market narratives",
                "GR": "regulation",
                "HEAD": "high-impact changes",
            },
            "uncertainty_rule": "Do not invent internal facts",
            "benchmark_scope": "must not become business context",
            "delivery_policy": {
                "status": "service metadata",
                "urgent": "critical signals bypass planned release",
            },
        }
    )

    assert "GS Labs" in result.common.text
    assert "subscriber devices" in result.common.text
    assert result.pr.text == "market narratives"
    assert result.gr.text == "regulation"
    assert result.version == "external-v1"
    assert "Получатель HEAD: high-impact changes" in result.common.text
    assert "service metadata" not in result.common.text
    assert "critical signals bypass planned release" in result.common.text


def document():
    return PreparedDocument(
        "m1", "Заголовок", "Минцифры внесло законопроект №123. Он затрагивает российское ПО."
    )


def answer(**overrides):
    raw = {
        "status": "ok",
        "reason": "",
        "signals": [
            {
                "summary": "Минцифры внесло законопроект №123.",
                "claims": [
                    {
                        "text": "Законопроект внесён.",
                        "evidence_quote": "Минцифры внесло законопроект №123.",
                    }
                ],
                "relevance": "relevant",
                "importance": "high",
                "interest": "GR",
                "recipient_roles": ["GR"],
                "impact": "Может затронуть реестровое ПО.",
                "urgency": "routine",
                "confidence": 0.9,
                "unknowns": [],
                "research_questions": [],
                "npa_identifier": "123",
                "npa_stage": "introduced",
                "npa_version": "v2",
                "npa_effective_from": "2026-12-01",
                "npa_change_summary": "Срок сокращён",
                "reasoning": "Прямое регулирование ПО.",
            }
        ],
    }
    raw.update(overrides)
    return raw


def test_one_pass_returns_grounded_signal():
    fake = FakeLLM(answer())
    result = PrimaryAnalyzer(fake, model="glm-5.3-flash:cloud").analyze(document(), context())
    assert result.status == "ok"
    assert result.calls == 1
    assert result.signals[0].roles == ["GR"]
    assert result.signals[0].claims[0].evidence_quote in document().text
    assert result.signals[0].npa_version == "v2"
    assert result.signals[0].npa_effective_from == "2026-12-01"


def test_two_pass_uses_two_calls_and_same_contract():
    extracted = {
        "status": "ok",
        "reason": "",
        "signals": [{"summary": "Минцифры внесло законопроект №123.", "claims": []}],
    }
    fake = FakeLLM([extracted, answer()])
    result = PrimaryAnalyzer(fake, model="glm-5.3-flash:cloud").analyze(
        document(), context(), mode="two_pass"
    )
    assert result.status == "ok"
    assert result.calls == 2


def test_ungrounded_claim_is_rejected_instead_of_silently_kept():
    raw = answer()
    raw["signals"][0]["claims"][0]["evidence_quote"] = "Этого нет в документе"
    result = PrimaryAnalyzer(FakeLLM(raw), model="glm-5.3-flash:cloud").analyze(
        document(), context()
    )
    assert result.status == "no_signal"
    assert result.signals == ()


def test_empty_material_does_not_call_model():
    fake = FakeLLM(answer())
    result = PrimaryAnalyzer(fake, model="glm-5.3-flash:cloud").analyze(
        PreparedDocument("m1", "", ""), context()
    )
    assert result.status == "unreadable"
    assert fake.calls == 0


def test_long_material_is_split_and_every_chunk_is_analysed():
    text = "Первая важная фраза. " + ("длинный текст " * 20) + "\n\nВторая важная фраза."

    def grounded(prompt):
        import json

        material = json.loads(prompt)["document"]["text"]
        quote = material[: min(25, len(material))]
        raw = answer()
        raw["signals"][0]["summary"] = quote
        raw["signals"][0]["claims"] = [{"text": quote, "evidence_quote": quote}]
        return raw

    fake = FakeLLM(grounded)
    result = PrimaryAnalyzer(fake, model="glm-5.3-flash:cloud", max_chars=120).analyze(
        PreparedDocument("long", "", text), context()
    )
    assert fake.calls > 1
    assert len(result.signals) == fake.calls
    assert [signal.signal_id for signal in result.signals] == [
        f"long:s{i}" for i in range(1, fake.calls + 1)
    ]


def test_temporary_provider_failure_becomes_visible_failed_analysis():
    class FailingProvider:
        def complete(self, prompt, schema, *, system=""):
            raise LlmTemporaryError("timeout")

    result = PrimaryAnalyzer(FailingProvider(), model="glm-5.3-flash:cloud").analyze(
        document(), context()
    )
    assert result.status == "failed"
    assert result.signals == ()
    assert "timeout" in result.reason
