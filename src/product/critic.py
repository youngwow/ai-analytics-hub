"""A4: optional GLM critic; original signal is never overwritten in storage."""

from __future__ import annotations

import json
import time
from dataclasses import replace

from ..processing.llm import LLMProvider
from .contracts import CriticResult, EvidenceClaim, PreparedDocument, SignalDraft

SCHEMA = {
    "type": "object",
    "required": ["issues", "corrections", "needs_human_review"],
    "properties": {
        "issues": {"type": "array", "items": {"type": "string"}},
        "needs_human_review": {"type": "boolean"},
        "corrections": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["text", "evidence_quote"],
                        "properties": {
                            "text": {"type": "string"},
                            "evidence_quote": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                },
                "importance": {"enum": ["low", "medium", "high", "critical"]},
                "interest": {"enum": ["PR", "GR", "BOTH", "IRRELEVANT"]},
                "impact": {"type": "string"},
                "urgency": {"enum": ["routine", "urgent"]},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}
SYSTEM = """Ты независимый критик AI-карточки. Верни только JSON.
Проверь факты, дословные evidence, атрибуцию, даты, числа, пропуски критичного и занижение
важности. Не добавляй внешние факты. Не уверен — needs_human_review=true.
corrections может содержать summary, claims, importance, interest, impact, urgency."""

SYSTEM += """
Точная форма ответа:
{"issues":[],"corrections":{},"needs_human_review":false}
Не добавляй verification, markdown, комментарии или текст вне JSON. В JSON используй только
допустимые литералы: true, false, null; не вставляй пояснения после значений.
"""


class SignalCritic:
    def __init__(self, provider: LLMProvider, *, model: str):
        self.provider = provider
        self.model = model

    def review(
        self, document: PreparedDocument, signal: SignalDraft, *, enabled: bool = True
    ) -> CriticResult:
        if not enabled:
            return CriticResult(signal.signal_id, (), signal, False, self.model, 0)
        started = time.monotonic()
        completion = self.provider.complete(
            json.dumps(
                {
                    "document": {"id": document.id, "title": document.title, "text": document.text},
                    "signal": {
                        "summary": signal.summary,
                        "claims": [x.__dict__ for x in signal.claims],
                        "importance": signal.importance,
                        "interest": signal.interest,
                        "impact": signal.impact,
                        "urgency": signal.urgency,
                    },
                },
                ensure_ascii=False,
            ),
            SCHEMA,
            system=SYSTEM,
        )
        corrections = completion.data.get("corrections") or {}
        claims = signal.claims
        if isinstance(corrections.get("claims"), list):
            valid: list[EvidenceClaim] = []
            for row in corrections["claims"]:
                if not isinstance(row, dict):
                    continue
                text = str(row.get("text") or "").strip()
                quote = str(row.get("evidence_quote") or "").strip()
                if text and quote and quote in document.text:
                    valid.append(EvidenceClaim(text, quote))
            if valid:
                claims = tuple(valid)
        importance = str(corrections.get("importance") or signal.importance)
        if importance not in {"low", "medium", "high", "critical"}:
            importance = signal.importance
        interest = str(corrections.get("interest") or signal.interest).upper()
        if interest not in {"PR", "GR", "BOTH", "IRRELEVANT"}:
            interest = signal.interest
        urgency = str(corrections.get("urgency") or signal.urgency).lower()
        if urgency not in {"routine", "urgent"}:
            urgency = signal.urgency
        corrected = replace(
            signal,
            summary=str(corrections.get("summary") or signal.summary),
            claims=claims,
            importance=importance,  # type: ignore[arg-type]
            interest=interest,  # type: ignore[arg-type]
            impact=str(corrections.get("impact") or signal.impact),
            urgency=urgency,
        )
        issues = tuple(str(x) for x in completion.data.get("issues", []) if str(x).strip())
        needs_review = bool(completion.data.get("needs_human_review", False))
        # Invalid corrected evidence must never silently replace a valid card.
        if corrections.get("claims") and claims == signal.claims:
            needs_review = True
            issues += ("critic corrections contained no grounded claims",)
        return CriticResult(
            signal.signal_id,
            issues,
            corrected,
            needs_review,
            completion.model or self.model,
            max(completion.latency_ms, int((time.monotonic() - started) * 1000)),
        )
