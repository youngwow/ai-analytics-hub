"""S2-S5 for one cluster: classify, extract, summarise, prioritise.

One structured-output call does S2-S5 (research.md, R-07) — a cluster of fifteen
reprints costs one call, not fifteen. Nothing here touches the database: the
pipeline takes text and returns a `Draft`, which is what makes it testable
against a fake provider.

Long documents are sent as a clipped prefix rather than map-reduced on purpose:
`evidence_offsets` are coordinates in `norm_text`, and a prefix keeps them valid
while a condensed retelling would not. Such cards carry `truncated` and a lower
confidence instead of silently claiming full coverage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import LLMConfig, ProcessingConfig
from ..models import CompanyProfile, LlmCall
from ..utils import get_logger
from . import normalize, prompts
from .llm import Completion, LlmConfigError, LlmError, LLMProvider
from .schema import RESULT_SCHEMA, InvalidResponse, ParsedResult, parse_result

log = get_logger("pipeline")

PRIORITY_ORDER = ("low", "medium", "high")
RETRY_HINT = (
    "\n\nПредыдущий ответ отклонён: {reason}. Верни ровно 3-5 предложений summary, "
    "по паре evidence_offsets на каждое предложение и числа в диапазоне 0-1."
)
TRUNCATED_NOTE = "Текст обрезан до лимита контекста, оценка по началу документа."


def bump(priority: str) -> str:
    """One step up the scale; `high` stays `high`. Never used to lower a priority."""
    index = PRIORITY_ORDER.index(priority) if priority in PRIORITY_ORDER else 1
    return PRIORITY_ORDER[min(index + 1, len(PRIORITY_ORDER) - 1)]


@dataclass
class Draft:
    """The card as the pipeline sees it, before it becomes a row."""

    type: str = "news"
    title: str = ""
    summary: list[str] = field(default_factory=list)
    entities: dict[str, str | None] = field(default_factory=dict)
    priority: str = "medium"
    relevance_score: float = 0.0
    reasoning: str = ""
    confidence: float = 0.0
    tags: list[str] = field(default_factory=list)
    matched_profile_facets: list[str] = field(default_factory=list)
    offsets: list[tuple[int, int]] = field(default_factory=list)
    npa_status: str | None = None
    npa_key: str | None = None
    degraded: bool = False
    needs_review: bool = False
    truncated: bool = False
    model_name: str = ""
    calls: list[LlmCall] = field(default_factory=list)

    @property
    def summary_text(self) -> str:
        return "\n".join(self.summary)


class Pipeline:
    """S2-S5 over one document's normalised text."""

    def __init__(
        self,
        config: ProcessingConfig,
        llm_config: LLMConfig,
        provider: LLMProvider | None,
    ):
        self.config = config
        self.llm = llm_config
        self.provider = provider

    def process(
        self,
        norm_text: str,
        *,
        title: str = "",
        source: str = "",
        published: str = "",
        profile: CompanyProfile | None = None,
    ) -> Draft:
        """Produce a card draft; fall back to the extractive baseline on failure."""
        text = normalize.clip(norm_text, self.config.max_chars)
        truncated = len(text) < len(norm_text)
        if self.provider is None:
            return self._baseline(norm_text, title, "нет провайдера модели", truncated)

        prompt = prompts.build(
            text=text,
            title=title,
            source=source,
            published=published,
            profile_block=profile.prompt_block() if profile else "",
        )
        calls: list[LlmCall] = []
        reason = ""
        for attempt in range(2):  # one structured call, one corrective retry
            body = prompt + (RETRY_HINT.format(reason=reason) if reason else "")
            try:
                completion = self._complete(body, calls)
            except LlmConfigError:
                raise
            except LlmError as e:
                log.warning("модель недоступна (%s), карточка деградирует", e)
                return self._baseline(norm_text, title, str(e), truncated, calls)
            try:
                result = parse_result(completion.data, text_length=len(text))
            except InvalidResponse as e:
                reason = str(e)
                calls[-1].status = "retry"
                calls[-1].error = reason
                log.warning("ответ модели отклонён: %s", reason)
                continue
            return self._draft(result, completion, calls, truncated)
        return self._baseline(norm_text, title, f"ответ не прошёл проверку: {reason}", truncated, calls)

    def _complete(self, prompt: str, calls: list[LlmCall]) -> Completion:
        started = time.monotonic()
        try:
            completion = self.provider.complete(
                prompt, RESULT_SCHEMA, system=prompts.system_prompt()
            )
        except LlmError as e:
            calls.append(
                LlmCall(
                    stage=prompts.STAGE,
                    model=self.llm.model,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    status="failed",
                    error=str(e)[:500],
                )
            )
            raise
        calls.append(
            LlmCall(
                stage=prompts.STAGE,
                model=completion.model or self.llm.model,
                tokens_in=completion.tokens_in,
                tokens_out=completion.tokens_out,
                latency_ms=completion.latency_ms,
                status="ok",
            )
        )
        return completion

    def _draft(
        self,
        result: ParsedResult,
        completion: Completion,
        calls: list[LlmCall],
        truncated: bool,
    ) -> Draft:
        summary, offsets = result.summary, result.evidence_offsets
        confidence = result.confidence * (0.7 if truncated else 1.0)
        priority, reasoning, needs_review = self._failsafe(result, confidence)
        if truncated:
            reasoning = f"{reasoning} {TRUNCATED_NOTE}".strip()
        return Draft(
            type=result.type,
            title=summary[0] if summary else result.title,
            summary=summary,
            entities=result.entities,
            priority=priority,
            relevance_score=result.relevance_score,
            reasoning=reasoning,
            confidence=confidence,
            tags=result.tags,
            matched_profile_facets=result.matched_profile_facets,
            needs_review=needs_review,
            offsets=offsets,
            npa_status=result.npa_status,
            npa_key=result.npa_key,
            truncated=truncated,
            model_name=completion.model or self.llm.model,
            calls=calls,
        )

    def _failsafe(self, result: ParsedResult, confidence: float) -> tuple[str, str, bool]:
        """Borderline relevance or low confidence raises the priority — never lowers it.

        Dropping a critical act into `low` costs more than one extra card in the
        morning review (spec, US-2). Тот же признак поднимает `needs_review`:
        именно эти карточки человек должен посмотреть глазами — раньше флаг
        считался, но никуда не записывался и всегда оставался нулём.
        """
        borderline = self.config.borderline_low <= result.relevance_score <= self.config.borderline_high
        unsure = confidence < 0.5
        needs_review = borderline or unsure
        if not needs_review or result.priority == "high":
            return result.priority, result.reasoning, needs_review
        raised = bump(result.priority)
        if raised == result.priority:
            return result.priority, result.reasoning, needs_review
        why = "пограничная релевантность" if borderline else "низкая уверенность модели"
        return raised, f"{result.reasoning} Приоритет повышен: {why}.".strip(), needs_review

    def _baseline(
        self,
        norm_text: str,
        title: str,
        why: str,
        truncated: bool = False,
        calls: list[LlmCall] | None = None,
    ) -> Draft:
        """Extractive fallback: the feed must not empty out because an API is down."""
        sentences = normalize.lead(norm_text, 3)
        offsets = [(s.start, s.end) for s in normalize.split_sentences(norm_text)[:3]]
        return Draft(
            type="news",
            title=title or (sentences[0] if sentences else ""),
            summary=sentences,
            entities={"who": None, "what": None, "when": None, "impact": None},
            priority="medium",
            relevance_score=0.0,
            reasoning=f"Карточка собрана без модели: {why}.",
            confidence=0.3,
            offsets=offsets,
            degraded=True,
            truncated=truncated,
            model_name="",
            calls=calls or [],
        )
