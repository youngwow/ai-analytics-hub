"""Frozen, explainable policy for routing risky signals to human review."""

from __future__ import annotations

from typing import Any

from .contracts import SignalDraft


def review_reasons(signal: SignalDraft) -> tuple[str, ...]:
    return _reasons(
        relevance=signal.relevance,
        importance=signal.importance,
        urgency=signal.urgency,
        kind=signal.kind,
        claims=signal.claims,
        unknowns=signal.unknowns,
        research_questions=signal.research_questions,
    )


def payload_review_reasons(signal: dict[str, Any]) -> tuple[str, ...]:
    return _reasons(
        relevance=str(signal.get("relevance") or "unknown"),
        importance=str(signal.get("importance") or "low"),
        urgency=str(signal.get("urgency") or "routine"),
        kind=str(signal.get("kind") or "news"),
        claims=signal.get("claims") or (),
        unknowns=signal.get("unknowns") or (),
        research_questions=signal.get("research_questions") or (),
    )


def _reasons(
    *,
    relevance: str,
    importance: str,
    urgency: str,
    kind: str,
    claims,
    unknowns,
    research_questions,
) -> tuple[str, ...]:
    reasons = []
    if importance == "critical" or urgency == "urgent":
        reasons.append("critical_or_urgent")
    if relevance in {"borderline", "unknown"}:
        reasons.append("uncertain_relevance")
    if unknowns or research_questions:
        reasons.append("open_questions")
    if kind == "npa_candidate":
        reasons.append("unconfirmed_npa")
    if not claims and relevance not in {"irrelevant"}:
        reasons.append("missing_grounded_claims")
    return tuple(reasons)
