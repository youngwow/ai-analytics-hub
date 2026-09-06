"""Resolve NPA signals into long-lived objects without benchmark-specific labels."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from ..processing.llm import LLMProvider, LlmTemporaryError
from .contracts import PreparedDocument, SignalDraft

NPA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["objects"],
    "properties": {
        "objects": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "object_id",
                    "member_signal_ids",
                    "external_id",
                    "current_stage",
                    "current_version",
                    "change_summary",
                    "effective_from",
                    "stale_signal_ids",
                    "needs_human_review",
                ],
                "properties": {
                    "object_id": {"type": "string"},
                    "member_signal_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "external_id": {"type": ["string", "null"]},
                    "current_stage": {"type": "string"},
                    "current_version": {"type": "string"},
                    "change_summary": {"type": "string"},
                    "effective_from": {"type": ["string", "null"]},
                    "stale_signal_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "needs_human_review": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}

ATTACH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["object_id", "relation", "confidence", "reason"],
    "properties": {
        "object_id": {"type": ["string", "null"]},
        "relation": {"enum": ["same_npa", "different"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "additionalProperties": False,
}

SYSTEM = """Ты ведёшь реестр НПА для GS Labs. Верни только JSON по схеме.
Сгруппируй сигналы только если они относятся к одному нормативному объекту. Одинаковая тема
не означает одинаковый НПА. Официальный идентификатор сильнее похожего названия. Официальные
источники определяют текущие стадию, редакцию и дату действия; вторичный источник может быть
пересказом или устаревшей версией и не должен перезаписывать официальное состояние.
Для уже отслеживаемого НПА используй ровно его object_id. Для нового объекта задай object_id
NEW:<external_id>, а без подтверждённого идентификатора — NEW:UNKNOWN:<короткий-id> и обязательно
needs_human_review=true. Каждый входной signal_id должен встретиться ровно в одном объекте.
Не придумывай стадию, версию, дату действия или изменение: если данных нет, пиши unknown/null.
current_stage использует только: draft, public_discussion, revised_draft, introduced,
adopted, effective, amended, repealed или unknown. current_version — внутренняя короткая метка редакции.
Для нового объекта используй v1_<ключевой_параметр>, если в тексте явно есть
отличительный срок, область или иной параметр. Для отслеживаемого объекта повышай
номер только при явной новой редакции. Суффикс строй только из доказанных входом
фактов, латиницей и snake_case; например 12 месяцев -> 12_months. Если такого факта нет
и номер редакции не указан, пиши unknown.
member_signal_ids включает и актуальные, и устаревшие упоминания; stale_signal_ids — только
устаревшие или вторичные записи, которые нельзя считать текущим состоянием. При совпадении ID
объедини все сигналы в один объект. Текущее состояние возьми из самого позднего по published_at
официального источника. Если официальный источник задаёт будущую дату начала действия, обязательно
верни её в effective_from как YYYY-MM-DD. Вторичный пересказ не входит в актуальную историю состояния
и должен попасть в stale_signal_ids, если официальный источник уже подтверждает более новую версию."""

ATTACH_SYSTEM = """Ты проверяешь, относится ли публикация без подтверждённого ID к одному уже
разрешённому НПА. Верни только JSON. same_npa допустимо лишь для того же конкретного документа:
совпадают предмет, участники процедуры, изменения и хронология; общей отраслевой темы недостаточно.
Новый URL, отсутствие ID в пересказе или противоречащая стадия не создают новый объект, если текст
явно описывает редакцию или пересказ того же документа. При сомнении верни different."""


@dataclass(frozen=True)
class NpaResolution:
    object_id: str
    member_signal_ids: tuple[str, ...]
    external_id: str | None
    current_stage: str
    current_version: str
    change_summary: str
    effective_from: str | None
    stale_signal_ids: tuple[str, ...] = ()
    needs_human_review: bool = False
    prior_history_ids: tuple[str, ...] = ()


class NpaResolver:
    def __init__(self, provider: LLMProvider, *, model: str):
        self.provider = provider
        self.model = model

    def resolve(
        self,
        signals: list[SignalDraft],
        documents: dict[str, PreparedDocument],
        tracked: list[dict[str, Any]],
    ) -> tuple[NpaResolution, ...]:
        if not signals:
            return ()
        payload = {
            "tracked_npas": tracked,
            "signals": [
                self._signal_payload(signal, documents[signal.material_id]) for signal in signals
            ],
        }
        try:
            completion = self.provider.complete(
                json.dumps(payload, ensure_ascii=False), NPA_SCHEMA, system=SYSTEM
            )
        except LlmTemporaryError:
            fallback = self._safe_fallback(signals, documents, tracked)
            return self._attach_unresolved(fallback, signals, documents, tracked)
        resolved = tuple(
            self._apply_source_authority(resolution, signals, documents)
            for resolution in self._validate(completion.data, signals, tracked)
        )
        return self._attach_unresolved(resolved, signals, documents, tracked)

    @staticmethod
    def _signal_payload(signal: SignalDraft, document: PreparedDocument) -> dict[str, Any]:
        return {
            "signal_id": signal.signal_id,
            "material_id": signal.material_id,
            "source_class": document.source_class,
            "source_name": document.source_name,
            "source_url": document.source_url,
            "published_at": document.published_at,
            "title": document.title,
            # The resolver needs identity and chronology, not an unbounded copy
            # of every source document. Keep enough source text for evidence
            # while protecting the model context from oversized publications.
            "text": document.text[:6000],
            "kind": signal.kind,
            "identifier": signal.npa_identifier,
            "stage": signal.npa_stage,
            "version": signal.npa_version,
            "effective_from": signal.npa_effective_from,
            "change_summary": signal.npa_change_summary,
            "summary": signal.summary,
            "claims": [
                {"text": claim.text, "evidence_quote": claim.evidence_quote}
                for claim in signal.claims
            ],
        }

    def _validate(
        self,
        data: dict[str, Any],
        signals: list[SignalDraft],
        tracked: list[dict[str, Any]],
    ) -> tuple[NpaResolution, ...]:
        known_signals = {signal.signal_id: signal for signal in signals}
        tracked_ids = {str(item.get("object_id")) for item in tracked if item.get("object_id")}
        tracked_by_id = {
            str(item["object_id"]): item for item in tracked if item.get("object_id")
        }
        tracked_by_external = {
            str(item["external_id"]).strip(): str(item["object_id"])
            for item in tracked
            if item.get("external_id") and item.get("object_id")
        }
        used: set[str] = set()
        result: dict[str, NpaResolution] = {}
        for raw in data.get("objects") or []:
            if not isinstance(raw, dict):
                continue
            members = tuple(
                dict.fromkeys(
                    str(item)
                    for item in raw.get("member_signal_ids") or []
                    if str(item) in known_signals and str(item) not in used
                )
            )
            if not members:
                continue
            object_id = str(raw.get("object_id") or "").strip()
            external_id = (
                str(raw.get("external_id")).strip() if raw.get("external_id") else None
            )
            if external_id in tracked_by_external:
                object_id = tracked_by_external[external_id]
            if object_id not in tracked_ids and not object_id.startswith("NEW:"):
                object_id = self._new_id(external_id, members[0])
            stale = tuple(
                str(item) for item in raw.get("stale_signal_ids") or [] if str(item) in members
            )
            candidate = NpaResolution(
                object_id=object_id,
                member_signal_ids=members,
                external_id=external_id,
                current_stage=self._stage(raw.get("current_stage")),
                current_version=str(raw.get("current_version") or "unknown").strip(),
                change_summary=str(raw.get("change_summary") or "unknown").strip(),
                effective_from=(
                    str(raw.get("effective_from")).strip()
                    if raw.get("effective_from")
                    else None
                ),
                stale_signal_ids=stale,
                needs_human_review=bool(raw.get("needs_human_review", False)),
                prior_history_ids=self._history_ids(tracked_by_id.get(object_id)),
            )
            result[object_id] = self._merge(result.get(object_id), candidate)
            used.update(members)
        missing = [signal for signal in signals if signal.signal_id not in used]
        for candidate in self._safe_fallback(missing, {}, tracked):
            result[candidate.object_id] = self._merge(
                result.get(candidate.object_id), candidate
            )
        return tuple(result.values())

    @classmethod
    def _safe_fallback(
        cls,
        signals: list[SignalDraft],
        documents: dict[str, PreparedDocument],
        tracked: list[dict[str, Any]],
    ) -> tuple[NpaResolution, ...]:
        tracked_by_external = {
            str(item["external_id"]).strip(): str(item["object_id"])
            for item in tracked
            if item.get("external_id") and item.get("object_id")
        }
        tracked_by_id = {
            str(item["object_id"]): item for item in tracked if item.get("object_id")
        }
        groups: dict[str, list[SignalDraft]] = {}
        for signal in signals:
            external_id = str(signal.npa_identifier or "").strip()
            object_id = tracked_by_external.get(external_id) or cls._new_id(
                external_id or None, signal.signal_id
            )
            groups.setdefault(object_id, []).append(signal)
        result = []
        for object_id, members in groups.items():
            official = [
                signal
                for signal in members
                if documents.get(signal.material_id)
                and documents[signal.material_id].source_class == "regulator"
            ]
            primary = max(
                official or members,
                key=lambda signal: str(
                    documents.get(signal.material_id).published_at
                    if documents.get(signal.material_id)
                    else ""
                ),
            )
            stale = tuple(
                signal.signal_id for signal in members if official and signal is not primary
                and (
                    not documents.get(signal.material_id)
                    or documents[signal.material_id].source_class != "regulator"
                )
            )
            tracked_record = tracked_by_id.get(object_id)
            result.append(
                NpaResolution(
                    object_id=object_id,
                    member_signal_ids=tuple(signal.signal_id for signal in members),
                    external_id=primary.npa_identifier,
                    current_stage=cls._stage(primary.npa_stage),
                    current_version=cls._fallback_version(primary, tracked_record),
                    change_summary=primary.npa_change_summary or "unknown",
                    effective_from=primary.npa_effective_from,
                    stale_signal_ids=stale,
                    needs_human_review=True,
                    prior_history_ids=cls._history_ids(tracked_by_id.get(object_id)),
                )
            )
        return tuple(result)

    @staticmethod
    def _merge(
        current: NpaResolution | None, candidate: NpaResolution
    ) -> NpaResolution:
        if current is None:
            return candidate
        return NpaResolution(
            object_id=current.object_id,
            member_signal_ids=tuple(
                dict.fromkeys((*current.member_signal_ids, *candidate.member_signal_ids))
            ),
            external_id=current.external_id or candidate.external_id,
            current_stage=(
                current.current_stage
                if current.current_stage != "unknown"
                else candidate.current_stage
            ),
            current_version=(
                current.current_version
                if current.current_version != "unknown"
                else candidate.current_version
            ),
            change_summary=(
                current.change_summary
                if current.change_summary != "unknown"
                else candidate.change_summary
            ),
            effective_from=current.effective_from or candidate.effective_from,
            stale_signal_ids=tuple(
                dict.fromkeys((*current.stale_signal_ids, *candidate.stale_signal_ids))
            ),
            needs_human_review=(
                current.needs_human_review or candidate.needs_human_review
            ),
            prior_history_ids=tuple(
                dict.fromkeys((*current.prior_history_ids, *candidate.prior_history_ids))
            ),
        )

    @classmethod
    def _apply_source_authority(
        cls,
        resolution: NpaResolution,
        signals: list[SignalDraft],
        documents: dict[str, PreparedDocument],
    ) -> NpaResolution:
        by_id = {signal.signal_id: signal for signal in signals}
        members = [by_id[item] for item in resolution.member_signal_ids if item in by_id]
        official = [
            signal
            for signal in members
            if documents.get(signal.material_id)
            and documents[signal.material_id].source_class == "regulator"
        ]
        if not official:
            return resolution
        latest_official = max(
            official,
            key=lambda signal: str(documents[signal.material_id].published_at or ""),
        )
        secondary = tuple(
            signal.signal_id
            for signal in members
            if not documents.get(signal.material_id)
            or documents[signal.material_id].source_class != "regulator"
        )
        latest_stage = cls._stage(latest_official.npa_stage)
        current_stage = (
            latest_stage if latest_stage != "unknown" else resolution.current_stage
        )
        current_version = resolution.current_version
        # A new object can arrive as an initial official publication followed
        # by a demonstrably changed official revision in the same collection
        # window.  The internal revision must then advance even when the source
        # did not assign a public version number.
        if (
            not resolution.prior_history_ids
            and len(official) > 1
            and latest_official.npa_change_summary
            and re.match(r"^v1(?:_|$)", current_version)
        ):
            current_version = re.sub(r"^v1", "v2", current_version, count=1)
        effective_from = latest_official.npa_effective_from or resolution.effective_from
        if current_stage not in {"adopted", "effective", "amended"}:
            effective_from = None
        return NpaResolution(
            object_id=resolution.object_id,
            member_signal_ids=resolution.member_signal_ids,
            external_id=resolution.external_id,
            current_stage=current_stage,
            current_version=current_version,
            change_summary=(
                latest_official.npa_change_summary or resolution.change_summary
            ),
            effective_from=effective_from,
            stale_signal_ids=secondary,
            needs_human_review=resolution.needs_human_review,
            prior_history_ids=resolution.prior_history_ids,
        )

    def _attach_unresolved(
        self,
        resolutions: tuple[NpaResolution, ...],
        signals: list[SignalDraft],
        documents: dict[str, PreparedDocument],
        tracked: list[dict[str, Any]],
    ) -> tuple[NpaResolution, ...]:
        tracked_ids = {
            str(item.get("object_id")) for item in tracked if item.get("object_id")
        }
        confirmed = {
            item.object_id: item
            for item in resolutions
            if item.external_id or item.object_id in tracked_ids
        }
        unresolved = [
            item
            for item in resolutions
            if not item.external_id and item.object_id not in tracked_ids
        ]
        if not confirmed or not unresolved:
            return resolutions
        by_signal = {signal.signal_id: signal for signal in signals}
        leftovers: list[NpaResolution] = []
        for candidate in unresolved:
            payload = {
                "candidate": self._resolution_payload(candidate, by_signal),
                "confirmed_objects": [
                    self._resolution_payload(item, by_signal)
                    for item in confirmed.values()
                ],
            }
            try:
                answer = self.provider.complete(
                    json.dumps(payload, ensure_ascii=False),
                    ATTACH_SCHEMA,
                    system=ATTACH_SYSTEM,
                ).data
            except LlmTemporaryError:
                leftovers.append(candidate)
                continue
            object_id = str(answer.get("object_id") or "")
            try:
                confidence = float(answer.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            if (
                answer.get("relation") == "same_npa"
                and object_id in confirmed
                and confidence >= 0.65
            ):
                confirmed[object_id] = self._apply_source_authority(
                    self._merge(confirmed[object_id], candidate), signals, documents
                )
            else:
                leftovers.append(candidate)
        return tuple((*confirmed.values(), *leftovers))

    @staticmethod
    def _resolution_payload(
        resolution: NpaResolution, signals: dict[str, SignalDraft]
    ) -> dict[str, Any]:
        return {
            "object_id": resolution.object_id,
            "external_id": resolution.external_id,
            "current_stage": resolution.current_stage,
            "current_version": resolution.current_version,
            "change_summary": resolution.change_summary,
            "effective_from": resolution.effective_from,
            "signals": [
                {
                    "signal_id": signal_id,
                    "title": signals[signal_id].source_title,
                    "summary": signals[signal_id].summary,
                    "claims": [claim.text for claim in signals[signal_id].claims],
                    "npa_stage": signals[signal_id].npa_stage,
                    "npa_version": signals[signal_id].npa_version,
                    "npa_change_summary": signals[signal_id].npa_change_summary,
                }
                for signal_id in resolution.member_signal_ids
                if signal_id in signals
            ],
        }

    @staticmethod
    def _history_ids(tracked: dict[str, Any] | None) -> tuple[str, ...]:
        if not tracked:
            return ()
        return tuple(
            str(item["event_id"])
            for item in tracked.get("history") or []
            if isinstance(item, dict) and item.get("event_id")
        )

    @staticmethod
    def _stage(value: Any) -> str:
        stage = str(value or "unknown").strip().lower()
        allowed = {
            "draft",
            "public_discussion",
            "revised_draft",
            "introduced",
            "adopted",
            "effective",
            "amended",
            "repealed",
            "unknown",
        }
        aliases = {
            "общественное обсуждение": "public_discussion",
            "проект, общественное обсуждение": "public_discussion",
            "принят": "adopted",
            "действует": "effective",
            "отменён": "repealed",
        }
        if stage in aliases:
            return aliases[stage]
        if stage in allowed:
            return stage
        if "принят" in stage and "не принят" not in stage:
            return "adopted"
        if "вступил в силу" in stage or "действует" in stage and "не действует" not in stage:
            return "effective"
        if "обсужден" in stage:
            return "public_discussion"
        if "доработ" in stage:
            return "revised_draft"
        if "согласован" in stage or "согласование" in stage:
            return "draft"
        return "unknown"

    @staticmethod
    def _fallback_version(
        signal: SignalDraft, tracked: dict[str, Any] | None
    ) -> str:
        label = str(signal.npa_version or "").strip()
        if not label:
            return "unknown"
        if re.match(r"^v\d+(?:_|$)", label):
            return label
        previous = str((tracked or {}).get("current_version") or "")
        match = re.match(r"^v(\d+)(?:_|$)", previous)
        number = int(match.group(1)) + 1 if match else 1
        safe = re.sub(r"[^a-zA-Z0-9а-яА-Я]+", "_", label).strip("_").lower()
        return f"v{number}_{safe}" if safe else f"v{number}"

    @classmethod
    def _safe_singleton(cls, signal: SignalDraft) -> NpaResolution:
        return NpaResolution(
            object_id=cls._new_id(signal.npa_identifier, signal.signal_id),
            member_signal_ids=(signal.signal_id,),
            external_id=signal.npa_identifier,
            current_stage=signal.npa_stage or "unknown",
            current_version="unknown",
            change_summary="unknown",
            effective_from=None,
            needs_human_review=True,
        )

    @staticmethod
    def _new_id(external_id: Any, seed: str) -> str:
        if external_id and str(external_id).strip():
            return f"NEW:{str(external_id).strip()}"
        digest = hashlib.sha256(seed.encode()).hexdigest()[:12]
        return f"NEW:UNKNOWN:{digest}"
