"""Build reviewable objects and delivery recommendations from product signals."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .contracts import AnalysisDraft, EventRecord, SignalDraft
from .npa import NpaResolution
from .review_policy import review_reasons

IMPORTANCE_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def item_decision(material_id: str, draft: AnalysisDraft) -> dict[str, Any]:
    if not draft.signals:
        irrelevant = draft.status == "irrelevant"
        return {
            "id": material_id,
            "relevance": "irrelevant" if irrelevant else "unknown",
            "importance": "low",
            "critical": False,
            "roles": [],
            "risk_flag": not irrelevant,
            "reason": draft.reason,
        }
    primary = max(draft.signals, key=lambda signal: IMPORTANCE_ORDER[signal.importance])
    roles = list(dict.fromkeys(role for signal in draft.signals for role in signal.roles))
    reasons = tuple(
        dict.fromkeys(reason for signal in draft.signals for reason in review_reasons(signal))
    )
    return {
        "id": material_id,
        "relevance": primary.relevance,
        "importance": primary.importance,
        "critical": any(signal.critical_or_escalate for signal in draft.signals),
        "roles": roles,
        "risk_flag": bool(reasons),
        "reason": "; ".join(reasons) or draft.reason,
    }


def event_object(
    event: EventRecord,
    signals: dict[str, SignalDraft],
    *,
    kind: str = "event",
) -> dict[str, Any] | None:
    members = [signals[signal_id] for signal_id in event.signal_ids if signal_id in signals]
    if not members:
        return None
    return _object_payload(event.id, kind, members)


def npa_object(resolution: NpaResolution, signals: dict[str, SignalDraft]) -> dict[str, Any] | None:
    members = [
        signals[signal_id] for signal_id in resolution.member_signal_ids if signal_id in signals
    ]
    if not members:
        return None
    result = _object_payload(resolution.object_id, "npa", members)
    result["npa_state"] = {
        "current_stage": resolution.current_stage,
        "current_version": resolution.current_version,
        "history_ids": list(
            dict.fromkeys(
                (
                    *resolution.prior_history_ids,
                    *(
                        signal.material_id
                        for signal in members
                        if signal.signal_id not in resolution.stale_signal_ids
                    ),
                )
            )
        ),
        "change_summary": resolution.change_summary,
        "effective_from": resolution.effective_from,
    }
    if resolution.needs_human_review:
        result["needs_human_review"] = True
    return result


def _object_payload(object_id: str, kind: str, signals: list[SignalDraft]) -> dict[str, Any]:
    primary = max(signals, key=lambda signal: IMPORTANCE_ORDER[signal.importance])
    roles = list(dict.fromkeys(role for signal in signals for role in signal.roles))
    claims = []
    seen = set()
    for signal in signals:
        for index, claim in enumerate(signal.claims, 1):
            key = (signal.material_id, claim.text, claim.evidence_quote)
            if key in seen:
                continue
            seen.add(key)
            claims.append(
                {
                    "claim_id": f"{signal.signal_id}:c{index}",
                    "text": claim.text,
                    "evidence": [
                        {"source_item_id": signal.material_id, "quote": claim.evidence_quote}
                    ],
                }
            )
    summaries = list(dict.fromkeys(signal.summary for signal in signals if signal.summary))
    impacts = list(dict.fromkeys(signal.impact for signal in signals if signal.impact))
    return {
        "object_id": object_id,
        "type": kind,
        "member_ids": list(dict.fromkeys(signal.material_id for signal in signals)),
        "summary": " ".join(summaries),
        "impact_on_gs_labs": " ".join(impacts),
        "importance": primary.importance,
        "critical": any(signal.critical_or_escalate for signal in signals),
        "roles": roles,
        "claims": claims,
    }


def build_deliveries(
    objects: Iterable[dict[str, Any]],
    *,
    allowed_object_ids: set[str] | None = None,
    scheduled_release: bool = False,
) -> list[dict[str, Any]]:
    """Create recommendations; human approval remains outside this function.

    ``allowed_object_ids`` separates the review queue from publication: an
    unresolved/borderline object may stay visible to the specialist without
    silently entering a release.  During a scheduled release, routine NPA
    updates are sections of the digest; outside it they remain explicit NPA
    notifications.
    """
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for obj in objects:
        if allowed_object_ids is not None and obj.get("object_id") not in allowed_object_ids:
            continue
        if not obj.get("roles") or not obj.get("claims"):
            continue
        if obj.get("critical"):
            delivery_type = "urgent_alert"
        elif obj.get("type") == "npa" and not scheduled_release:
            delivery_type = "npa_update"
        else:
            delivery_type = "planned_digest"
        for role in obj["roles"]:
            buckets[(delivery_type, role)].append(obj)
    result = []
    for (delivery_type, role), rows in sorted(buckets.items()):
        result.append(
            {
                "delivery_type": delivery_type,
                "recipient": role,
                "object_ids": [row["object_id"] for row in rows],
                "digest_text": "\n".join(f"• {row['summary']}" for row in rows),
            }
        )
    return result
