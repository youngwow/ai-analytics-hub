"""A3: event candidate retrieval and GLM-only final linking decision."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Literal, Sequence

from ..processing.llm import EmbeddingProvider, LLMProvider, LlmTemporaryError
from .contracts import EventRecord, LinkDecision, SignalDraft

LINK_SCHEMA = {
    "type": "object",
    "required": ["event_id", "relation", "confidence", "evidence", "needs_human_review"],
    "properties": {
        "event_id": {"type": ["string", "null"]},
        "relation": {
            "enum": ["same_event", "event_update", "related_topic", "different"]
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "string"},
        "needs_human_review": {"type": "boolean"},
    },
    "additionalProperties": False,
}
SYSTEM = """Ты связываешь новый сигнал с банком событий. Верни только JSON.
Допустимые relation: same_event, event_update, related_topic, different.
Не объединяй лишь из-за общей темы. same_event/event_update требуют одной конкретной истории и
совместимой хронологии. Объединяй первичное сообщение, уточнение причины и итог одного инцидента. Можно
объединить разные измерения одного рынка и периода в одну аналитическую историю, но нельзя терять различия методик.
Комментарий эксперта, ассоциации или участника рынка о том же конкретном проекте, пилоте или
документе относится к тому же событию; позиция остаётся отдельным claim со своим источником.
Разные программы, проекты и инициативы не становятся одним событием из-за общей лексики. Разные позиции сохраняй,
а сомнение отправляй человеку.
Форма ответа ровно одна:
{"event_id":"точный id одного кандидата или null","relation":"same_event|event_update|related_topic|different","confidence":0.0,"evidence":"краткое обоснование","needs_human_review":false}.
Не возвращай links, candidate_id, rationale или массив. Если нет same_event/event_update,
верни event_id=null; related_topic не объединяется с кандидатом."""


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return -1.0
    nl = math.sqrt(sum(x * x for x in left))
    nr = math.sqrt(sum(x * x for x in right))
    return sum(a * b for a, b in zip(left, right)) / (nl * nr) if nl and nr else -1.0


class EventLinker:
    def __init__(self, provider: LLMProvider, embedder: EmbeddingProvider | None, *, model: str):
        self.provider = provider
        self.embedder = embedder
        self.model = model
        self._embedding_cache: dict[tuple[str, int], tuple[float, ...]] = {}

    def ensure_embeddings(self, events: list[EventRecord]) -> list[EventRecord]:
        hydrated = [
            replace(event, embedding=self._embedding_cache[(event.id, event.version)])
            if not event.embedding and (event.id, event.version) in self._embedding_cache
            else event
            for event in events
        ]
        missing = [event for event in hydrated if not event.embedding]
        if not missing:
            return hydrated
        if self.embedder is None:
            raise ValueError("A3 embeddings mode requires an embedding provider")
        vectors = self.embedder.embed([event.compact_text for event in missing])
        by_id = {event.id: tuple(vector) for event, vector in zip(missing, vectors)}
        for event in missing:
            if event.id in by_id:
                self._embedding_cache[(event.id, event.version)] = by_id[event.id]
        return [
            replace(event, embedding=by_id[event.id]) if event.id in by_id else event
            for event in hydrated
        ]

    def link(
        self,
        signal: SignalDraft,
        events: list[EventRecord],
        *,
        mode: Literal["full_scan", "embedding_top20"] = "embedding_top20",
    ) -> LinkDecision:
        if not events:
            return LinkDecision(signal.signal_id, None, "different", 1.0, "event bank is empty")
        candidates = events
        if mode == "embedding_top20":
            events = self.ensure_embeddings(events)
            if self.embedder is None:
                raise ValueError("embedding provider is required")
            signal_vector = self.embedder.embed([self._signal_text(signal)])[0]
            candidates = sorted(events, key=lambda event: cosine(signal_vector, event.embedding), reverse=True)[:20]
        elif mode != "full_scan":
            raise ValueError(f"unknown A3 mode: {mode}")
        try:
            completion = self.provider.complete(
                json.dumps(
                    {
                        "task": "link_signal_to_event",
                        "signal": {"id": signal.signal_id, "text": self._signal_text(signal)},
                        "candidates": [
                            {
                                "id": e.id,
                                "title": e.title,
                                "summary": e.summary,
                                "text": e.compact_text,
                            }
                            for e in candidates
                        ],
                    },
                    ensure_ascii=False,
                ),
                LINK_SCHEMA,
                system=SYSTEM,
            )
        except LlmTemporaryError as exc:
            return LinkDecision(
                signal.signal_id,
                None,
                "different",
                0.0,
                f"temporary model failure: {exc}",
                True,
            )
        raw = completion.data
        relation = str(raw.get("relation") or "different")
        if relation not in {"same_event", "event_update", "related_topic", "different"}:
            relation = "different"
        candidate_ids = {event.id for event in candidates}
        event_id = raw.get("event_id")
        event_id = str(event_id) if event_id is not None else None
        if relation == "different" or event_id not in candidate_ids:
            event_id, relation = None, "different"
        try:
            confidence = min(1.0, max(0.0, float(raw.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        return LinkDecision(
            signal.signal_id,
            event_id,
            relation,  # type: ignore[arg-type]
            confidence,
            str(raw.get("evidence") or ""),
            bool(raw.get("needs_human_review", False)) or confidence < 0.65,
        )

    @staticmethod
    def _signal_text(signal: SignalDraft) -> str:
        claims = " ".join(claim.text for claim in signal.claims)
        return f"{signal.source_title}\n{signal.summary}\n{claims}\n{signal.impact}".strip()
