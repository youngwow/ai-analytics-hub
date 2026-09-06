"""A1: primary AI analysis in one-pass and two-pass configurations."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any, Literal

from ..processing.llm import LLMProvider, LlmTemporaryError
from .contracts import AnalysisDraft, EvidenceClaim, GsLabsContext, PreparedDocument, SignalDraft
from .preparation import chunks_with_offsets

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "signals", "reason"],
    "properties": {
        "status": {"enum": ["ok", "no_signal", "irrelevant", "unreadable"]},
        "reason": {"type": "string"},
        "signals": {"type": "array", "items": {"type": "object"}},
    },
}

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "signals", "reason"],
    "properties": {
        "status": {"enum": ["ok", "no_signal", "unreadable"]},
        "reason": {"type": "string"},
        "signals": {"type": "array", "items": {"type": "object"}},
    },
}

ASSESSMENT_SCHEMA = ANALYSIS_SCHEMA

SYSTEM = """Ты анализируешь внешние материалы для GS Labs. Возвращай только JSON.
Источник является единственным основанием фактов: контекст компании задаёт только релевантность.
Каждый claim обязан иметь короткую дословную evidence_quote из текста. Не додумывай.
Один материал может содержать 0..N самостоятельных сигналов. Критичное нельзя занижать в low.
interest: PR, GR, BOTH или IRRELEVANT. recipient_roles — массив из PR, GR, HEAD;
HEAD выбирается отдельно по управленческой значимости, а не автоматически из importance.
importance: low, medium, high или critical.
relevance: relevant, borderline, irrelevant или unknown. urgency: routine или urgent.
kind: news, npa или npa_candidate. npa используй для самого формального нормативно-правового акта,
проекта закона, постановления или приказа, а также для любой публикации, которая явно
ссылается на такой объект по официальному идентификатору: все версии и пересказы должны попасть
в одну историю НПА. Добровольный технический стандарт, запись реестра сертификатов, надзорное
действие или инцидент не являются НПА сами по себе и остаются news. npa_candidate используй,
когда материал вероятно относится к НПА, но его официальный идентификатор не подтверждён;
это кандидат на ручную проверку, а не разрешение создать подтверждённый объект НПА.
Если данных не хватает, перечисли unknowns и точные research_questions."""

SYSTEM += """
Границы оценки:
- relevant: есть прямая связь с продуктами, рынками, регуляторными рисками или репутацией GS Labs;
- конкретный проект оператора, конкурента или партнёра в основном продуктовом домене GS Labs
  является relevant и обычно не ниже medium, даже если GS Labs прямо не названа;
- borderline: связь правдоподобна, но прямое влияние или применимость не подтверждены;
- irrelevant: связи с заданным контекстом нет;
- unknown: материал может быть важен, но для решения не хватает ключевого факта.
- critical: подтверждённое регуляторное действие, инцидент безопасности или публичный кризис
  в основном домене компании со сроком не более 30 дней и риском остановки, санкции, активного
  обязательства или необходимостью
  немедленной проверки. Неизвестная внутренняя применимость не понижает такой сигнал: её нужно
  явно вынести в unknowns и отправить на срочную проверку.
- уязвимость внешней библиотеки или компонента при неизвестном использовании в GS Labs —
  high/unknown и ручная проверка, но не critical/urgent. Она становится critical только при
  подтверждённой зависимости, активной эксплуатации или факте затронутых продуктов. Без публичной
  связи с GS Labs такой сигнал не получает PR автоматически.
- официальное уточнение не понижает критичность активного события, если срок или обязательство
  остаются; оно исправляет факты и должно быть связано с исходным событием.
- публичное существенное искажение такого события интересно PR и GR; подтверждённое критичное
  событие также получает HEAD. Не назначай роли только по уровню importance — объясни их задачей.
- HEAD получает также масштабный пилот, крупный коммерческий запуск или существенное техническое
  изменение оператора в основном продуктовом домене, если оно влияет на рыночное или продуктовое
  решение. Не отправляй HEAD небольшой локальный контракт или обычный пересказ только из-за того,
  что это коммерческий проект.
- новый НПА уровня high с ограниченным окном обсуждения, существенными затратами на исполнение
  или возможным влиянием на основной продуктовый контур получает GR и HEAD.
- изменение стадии или редакции уже отслеживаемого НПА с явным ID является relevant; если оно
  затрагивает основной продуктовый контур или создаёт окно реакции, обычно это high для GR и HEAD.
- устаревший вторичный пересказ НПА остаётся на проверке и связывается с объектом, но сам по себе
  не становится critical и не получает PR/HEAD без доказанного широкого публичного кризиса.
- проект и обновление НПА обычно получают GR, а при уровне high также HEAD. PR добавляется только
  при явном публичном нарративе, репутационном риске или необходимости внешней коммуникации,
  а не из-за самого факта публикации документа.
- дедлайн общественного обсуждения, конкурса, закупки или подачи заявки обычно high/routine:
  близкая дата сама по себе не делает материал critical или urgent. urgent означает, что ожидание
  планового дайджеста реально создаёт риск; несколько дней на обычное действие — не срочный алерт.
- прямые новости GS Labs о компании, продукте, программе или крупном пилоте получают PR и HEAD.
  Существенная рыночная возможность в основном домене уровня high также получает HEAD.
- субсидии, гранты, льготы, программы господдержки и государственные закупки оценивает GR;
  при существенной потенциальной выгоде или ограниченном окне участия добавляй HEAD. PR нужен
  только при отдельной коммуникационной или репутационной задаче. Если право GS Labs на участие
  не подтверждено, явно сохрани это в unknowns и не описывай возможность как доступную компании.
- публичное предложение о будущих правилах рынка интересно GR и PR, даже если пока добровольно;
  не выдавай его за принятое регулирование.
- старый архивный материал без нового события, действующего статуса или текущего решения irrelevant,
  даже если его тема когда-то относилась к продуктам компании.
"""

SYSTEM += """
Форма ответа:
{"status":"ok|no_signal|irrelevant|unreadable","reason":"...","signals":[{
"signal_id":"необязательно","kind":"news|npa|npa_candidate","summary":"...",
"claims":[{"text":"атомарный факт","evidence_quote":"дословная цитата"}],
"relevance":"relevant|borderline|irrelevant|unknown",
"importance":"low|medium|high|critical","interest":"PR|GR|BOTH|IRRELEVANT",
"recipient_roles":["PR|GR|HEAD"],
"impact":"...","urgency":"routine|urgent","confidence":0.0,
"unknowns":[],"research_questions":[],"npa_identifier":null,"npa_stage":null,
"npa_version":null,"npa_effective_from":null,"npa_change_summary":null,
"reasoning":"..."}]}
Если самостоятельного сигнала нет, signals должен быть пустым массивом."""

SYSTEM += """
Пиши компактно: не более 5 ключевых claims на сигнал, summary — 1–2 предложения,
impact — не более 3 предложений, unknowns и research_questions — максимум по 3,
reasoning — одно короткое предложение. Сохраняй различия версий, сроков и позиций источников."""

SYSTEM += """
unknowns и research_questions содержат только пробелы, которые могут изменить включение,
приоритет, адресата или формулировку карточки. Не добавляй стандартные вопросы о поставщике,
партнёрстве или внутренних планах, если известных фактов уже достаточно для корректного дайджеста."""


def _doc_payload(document: PreparedDocument) -> dict[str, Any]:
    return {
        "id": document.id,
        "title": document.title,
        "text": document.text,
        "source_name": document.source_name,
        "source_type": document.source_type,
        "source_url": document.source_url,
        "published_at": document.published_at,
        "direction": document.direction,
        "completeness": document.completeness,
        "warnings": list(document.warnings),
        "source_class": document.source_class,
    }


class AnalysisError(ValueError):
    pass


class PrimaryAnalyzer:
    def __init__(self, provider: LLMProvider, *, model: str, max_chars: int = 12000):
        self.provider = provider
        self.model = model
        self.max_chars = max_chars

    def analyze(
        self,
        document: PreparedDocument,
        context: GsLabsContext,
        *,
        mode: Literal["one_pass", "two_pass"] = "one_pass",
    ) -> AnalysisDraft:
        if not document.id or not document.text:
            return AnalysisDraft(
                material_id=document.id,
                status="unreadable",
                signals=(),
                configuration_id=f"A1:{mode}",
                model=self.model,
                context_version=context.version,
                calls=0,
                reason="empty material id or text",
            )
        started = time.monotonic()
        chunks = document.chunks or chunks_with_offsets(document.text, self.max_chars)
        all_signals: list[SignalDraft] = []
        all_completions = []
        reasons: list[str] = []
        statuses: list[str] = []
        for chunk in chunks:
            part = replace(document, text=chunk.text, chunks=())
            try:
                status, reason, signals, completions = self._analyze_part(
                    part, context, mode
                )
            except LlmTemporaryError as exc:
                status, reason, signals, completions = (
                    "failed",
                    f"temporary model failure: {exc}",
                    (),
                    [],
                )
            statuses.append(status)
            if reason:
                reasons.append(reason)
            for signal in signals:
                all_signals.append(
                    replace(signal, signal_id=f"{document.id}:s{len(all_signals) + 1}")
                )
            all_completions.extend(completions)
        signals = tuple(all_signals)
        status = "ok" if signals else (
            "irrelevant" if statuses and all(x == "irrelevant" for x in statuses) else
            "failed" if "failed" in statuses else
            "unreadable" if statuses and all(x == "unreadable" for x in statuses) else
            "no_signal"
        )
        return AnalysisDraft(
            material_id=document.id,
            status=status,
            signals=signals,
            configuration_id=f"A1:{mode}",
            model=self.model,
            context_version=context.version,
            calls=len(all_completions),
            input_tokens=sum(x.tokens_in for x in all_completions),
            output_tokens=sum(x.tokens_out for x in all_completions),
            latency_ms=max(int((time.monotonic() - started) * 1000), sum(x.latency_ms for x in all_completions)),
            reason="; ".join(dict.fromkeys(reasons)),
        )

    def _analyze_part(self, document, context, mode):
        if mode == "one_pass":
            completion = self.provider.complete(
                self._one_pass_prompt(document, context), ANALYSIS_SCHEMA, system=SYSTEM
            )
            signals = self._parse_signals(completion.data, document)
            status, reason = self._status(completion.data, signals)
            return status, reason, signals, [completion]
        if mode == "two_pass":
            first = self.provider.complete(
                self._extract_prompt(document), EXTRACTION_SCHEMA, system=SYSTEM
            )
            extracted = first.data.get("signals") if isinstance(first.data, dict) else None
            if not extracted:
                status, reason = self._status(first.data, ())
                return status, reason, (), [first]
            second = self.provider.complete(
                self._assessment_prompt(document, context, extracted),
                ASSESSMENT_SCHEMA,
                system=SYSTEM,
            )
            signals = self._parse_signals(second.data, document)
            status, reason = self._status(second.data, signals)
            return status, reason, signals, [first, second]
        raise ValueError(f"unknown A1 mode: {mode}")

    def _one_pass_prompt(self, document: PreparedDocument, context: GsLabsContext) -> str:
        doc = _doc_payload(document)
        doc["text"] = document.text[: self.max_chars]
        payload = {"task": "extract_and_assess", "document": doc, "context": context.prompt_payload()}
        return json.dumps(payload, ensure_ascii=False)

    def _extract_prompt(self, document: PreparedDocument) -> str:
        doc = _doc_payload(document)
        doc["text"] = document.text[: self.max_chars]
        return json.dumps(
            {
                "task": "extract_context_independent_signals",
                "document": doc,
                "output_fields": ["summary", "claims", "unknowns"],
            },
            ensure_ascii=False,
        )

    def _assessment_prompt(self, document: PreparedDocument, context: GsLabsContext, extracted: Any) -> str:
        return json.dumps(
            {
                "task": "assess_extracted_signals",
                "document_id": document.id,
                "document_text": document.text[: self.max_chars],
                "signals": extracted,
                "context": context.prompt_payload(),
            },
            ensure_ascii=False,
        )

    def _status(self, data: dict[str, Any], signals: tuple[SignalDraft, ...]):
        if signals:
            return "ok", str(data.get("reason") or "")
        status = str(data.get("status") or "no_signal")
        if status not in {"no_signal", "irrelevant", "unreadable"}:
            status = "no_signal"
        return status, str(data.get("reason") or "no valid signals")

    def _parse_signals(self, data: dict[str, Any], document: PreparedDocument) -> tuple[SignalDraft, ...]:
        if not isinstance(data, dict):
            raise AnalysisError("analysis response is not an object")
        result: list[SignalDraft] = []
        for index, raw in enumerate(data.get("signals") or []):
            if not isinstance(raw, dict):
                continue
            claims: list[EvidenceClaim] = []
            for claim in raw.get("claims") or []:
                if not isinstance(claim, dict):
                    continue
                quote = str(claim.get("evidence_quote") or "").strip()
                text = str(claim.get("text") or "").strip()
                if text and quote and quote in document.text:
                    claims.append(EvidenceClaim(text=text, evidence_quote=quote))
            summary = str(raw.get("summary") or "").strip()
            if not summary or not claims:
                continue
            relevance = str(raw.get("relevance") or "unknown")
            importance = str(raw.get("importance") or "medium")
            interest = str(raw.get("interest") or "IRRELEVANT").upper()
            urgency = str(raw.get("urgency") or "routine").lower()
            kind = str(raw.get("kind") or "news").lower()
            if relevance not in {"relevant", "borderline", "irrelevant", "unknown"}:
                relevance = "unknown"
            if importance not in {"low", "medium", "high", "critical"}:
                importance = "medium"
            if interest not in {"PR", "GR", "BOTH", "IRRELEVANT"}:
                interest = "IRRELEVANT"
            if urgency not in {"routine", "urgent"}:
                urgency = "routine"
            if kind not in {"news", "npa", "npa_candidate"}:
                kind = "news"
            confidence = raw.get("confidence", 0.5)
            try:
                confidence = min(1.0, max(0.0, float(confidence)))
            except (TypeError, ValueError):
                confidence = 0.5
            if urgency == "urgent" and importance == "low":
                importance = "medium"
            roles = tuple(
                dict.fromkeys(
                    str(role).upper() for role in raw.get("recipient_roles", [])
                    if str(role).upper() in {"PR", "GR", "HEAD"}
                )
            )
            result.append(
                SignalDraft(
                    signal_id=str(raw.get("signal_id") or f"{document.id}:s{index + 1}"),
                    material_id=document.id,
                    summary=summary,
                    claims=tuple(claims),
                    relevance=relevance,  # type: ignore[arg-type]
                    importance=importance,  # type: ignore[arg-type]
                    interest=interest,  # type: ignore[arg-type]
                    impact=str(raw.get("impact") or "").strip(),
                    urgency=urgency,
                    confidence=confidence,
                    unknowns=tuple(str(x).strip() for x in raw.get("unknowns", []) if str(x).strip()),
                    research_questions=tuple(str(x).strip() for x in raw.get("research_questions", []) if str(x).strip()),
                    npa_identifier=(str(raw.get("npa_identifier")).strip() if raw.get("npa_identifier") else None),
                    npa_stage=(str(raw.get("npa_stage")).strip() if raw.get("npa_stage") else None),
                    npa_version=(
                        str(raw.get("npa_version")).strip()
                        if raw.get("npa_version")
                        else None
                    ),
                    npa_effective_from=(
                        str(raw.get("npa_effective_from")).strip()
                        if raw.get("npa_effective_from")
                        else None
                    ),
                    npa_change_summary=(
                        str(raw.get("npa_change_summary")).strip()
                        if raw.get("npa_change_summary")
                        else None
                    ),
                    reasoning=str(raw.get("reasoning") or "").strip(),
                    kind=kind,  # type: ignore[arg-type]
                    recipient_roles=roles,  # type: ignore[arg-type]
                    source_title=document.title,
                )
            )
        return tuple(result)
