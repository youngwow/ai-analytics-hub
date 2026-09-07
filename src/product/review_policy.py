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


def should_surface_in_work_queue(signal: dict[str, Any]) -> bool:
    """Return whether an unresolved signal deserves a PR/GR decision now.

    The append-only bank keeps every grounded signal. The work queue is a
    deliberately smaller product projection: background and already rejected
    material must not accumulate into an endless human inbox.
    """
    relevance = str(signal.get("relevance") or "unknown")
    interest = str(signal.get("interest") or "IRRELEVANT").upper()
    importance = str(signal.get("importance") or "low")
    urgency = str(signal.get("urgency") or "routine")

    if relevance == "irrelevant" or interest == "IRRELEVANT":
        return False
    if importance == "low" and urgency != "urgent":
        return False
    return importance in {"medium", "high", "critical"} or urgency == "urgent"


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
