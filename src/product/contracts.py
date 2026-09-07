"""Stable contracts for the target headless product contour.

These dataclasses deliberately contain no provider or transport details. Every
derived object keeps the material id and verbatim evidence, so a benchmark or
UI can always walk back to the original input.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

Interest = Literal["PR", "GR", "BOTH", "IRRELEVANT"]
Importance = Literal["low", "medium", "high", "critical"]
Relevance = Literal["relevant", "borderline", "irrelevant", "unknown"]


@dataclass(frozen=True)
class ContextBlock:
    text: str = ""
    important_examples: tuple[str, ...] = ()
    unimportant_examples: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "ContextBlock":
        value = value or {}
        return cls(
            text=str(value.get("text") or "").strip(),
            important_examples=tuple(str(x).strip() for x in value.get("important_examples", []) if str(x).strip()),
            unimportant_examples=tuple(str(x).strip() for x in value.get("unimportant_examples", []) if str(x).strip()),
        )


@dataclass(frozen=True)
class GsLabsContext:
    version: str
    common: ContextBlock
    pr: ContextBlock
    gr: ContextBlock

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GsLabsContext":
        # B3 and API clients use a flatter company/interests/roles profile.
        # Convert the visible context only; benchmark labels are never involved.
        if "company" in value or "interests" in value or "roles" in value:
            roles = value.get("roles") or {}
            common_lines = [
                value.get("company", ""),
                value.get("relation", ""),
                *(value.get("interests") or []),
                value.get("uncertainty_rule", ""),
                value.get("npa_policy", ""),
                f"Получатель HEAD: {roles.get('HEAD')}" if roles.get("HEAD") else "",
            ]
            delivery = value.get("delivery_policy") or {}
            if isinstance(delivery, dict):
                common_lines.extend(
                    str(item) for key, item in delivery.items() if key != "status"
                )
            return cls(
                version=str(value.get("version") or "external-v1"),
                common=ContextBlock(
                    "\n".join(
                        str(item).strip() for item in common_lines if str(item).strip()
                    )
                ),
                pr=ContextBlock(str(roles.get("PR") or "")),
                gr=ContextBlock(str(roles.get("GR") or "")),
            )
        # B2 v1 stores a compact known/unknowns shape. Convert it without adding
        # facts; production context already uses common/pr/gr blocks.
        if all(key in value for key in ("common", "pr", "gr")):
            return cls(
                version=str(value.get("version") or "unversioned"),
                common=ContextBlock.from_dict(value.get("common")),
                pr=ContextBlock.from_dict(value.get("pr")),
                gr=ContextBlock.from_dict(value.get("gr")),
            )
        known = value.get("known") or {}
        roles = known.get("roles") or {}
        common_lines = [
            known.get("company", ""),
            *(known.get("activities") or []),
            *(known.get("monitoring_goals") or []),
            f"Получатель HEAD: {roles.get('HEAD')}" if roles.get("HEAD") else "",
        ]
        boundaries = tuple(str(x) for x in value.get("negative_boundaries", []))
        unknowns = tuple(f"Неизвестно: {x}" for x in value.get("unknowns", []))
        return cls(
            version=str(value.get("version") or "unversioned"),
            common=ContextBlock("\n".join(str(x) for x in common_lines if x), (), boundaries + unknowns),
            pr=ContextBlock(str(roles.get("PR") or "")),
            gr=ContextBlock(str(roles.get("GR") or "")),
        )

    def prompt_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PreparedChunk:
    index: int
    text: str
    source_start: int
    source_end: int


@dataclass(frozen=True)
class PreparedDocument:
    id: str
    title: str
    text: str
    source_name: str = ""
    source_type: str = ""
    source_url: str | None = None
    published_at: str | None = None
    language: str = "ru"
    direction: Literal["PR", "GR", "BOTH", "UNKNOWN"] = "UNKNOWN"
    completeness: str = "full"
    warnings: tuple[str, ...] = ()
    chunks: tuple[PreparedChunk, ...] = ()
    source_class: str = "ordinary"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PreparedDocument":
        return cls(
            id=str(value.get("id") or "").strip(),
            title=str(value.get("title") or "").strip(),
            text=str(value.get("text") or "").strip(),
            source_name=str(value.get("source_name") or "").strip(),
            source_type=str(value.get("source_type") or "").strip(),
            source_url=value.get("source_url"),
            published_at=value.get("published_at"),
            language=str(value.get("language") or "ru"),
            direction=(
                str(value.get("direction") or "UNKNOWN").upper()
                if str(value.get("direction") or "UNKNOWN").upper()
                in {"PR", "GR", "BOTH", "UNKNOWN"}
                else "UNKNOWN"
            ),  # type: ignore[arg-type]
            completeness=str(value.get("completeness") or "full"),
            warnings=tuple(str(x) for x in value.get("warnings", [])),
            chunks=tuple(
                PreparedChunk(
                    int(row["index"]), str(row["text"]),
                    int(row["source_start"]), int(row["source_end"]),
                )
                for row in value.get("chunks", []) if isinstance(row, dict)
            ),
            source_class=str(value.get("source_class") or "ordinary").lower(),
        )


@dataclass(frozen=True)
class EvidenceClaim:
    text: str
    evidence_quote: str


@dataclass(frozen=True)
class SignalDraft:
    signal_id: str
    material_id: str
    summary: str
    claims: tuple[EvidenceClaim, ...]
    relevance: Relevance
    importance: Importance
    interest: Interest
    impact: str
    urgency: str
    confidence: float
    unknowns: tuple[str, ...] = ()
    research_questions: tuple[str, ...] = ()
    npa_identifier: str | None = None
    npa_stage: str | None = None
    npa_version: str | None = None
    npa_effective_from: str | None = None
    npa_change_summary: str | None = None
    reasoning: str = ""
    kind: Literal["news", "npa", "npa_candidate"] = "news"
    recipient_roles: tuple[Literal["PR", "GR", "HEAD"], ...] = ()
    source_title: str = ""

    @property
    def critical_or_escalate(self) -> bool:
        return self.importance == "critical" or self.urgency == "urgent"

    @property
    def roles(self) -> list[str]:
        # Product policy: an actually critical/urgent signal belongs to the
        # shared critical core.  It must not disappear from either specialist
        # view because the model selected only one functional profile.
        if self.critical_or_escalate:
            return ["PR", "GR", "HEAD"]
        if self.recipient_roles:
            return list(dict.fromkeys(self.recipient_roles))
        if self.interest == "BOTH":
            return ["PR", "GR"]
        if self.interest in ("PR", "GR"):
            return [self.interest]
        return []


@dataclass(frozen=True)
class AnalysisDraft:
    material_id: str
    status: Literal["ok", "no_signal", "irrelevant", "unreadable", "failed"]
    signals: tuple[SignalDraft, ...]
    configuration_id: str
    model: str
    context_version: str
    calls: int
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    reason: str = ""


@dataclass(frozen=True)
class SearchEvidence:
    url: str
    title: str
    snippet: str
    source_name: str = ""
    published_at: str | None = None


@dataclass(frozen=True)
class ResearchReport:
    signal_id: str
    status: Literal["complete", "incomplete", "failed", "not_needed"]
    mode: Literal["wide", "deep", "mixed", "none"]
    questions: tuple[str, ...]
    queries: tuple[str, ...]
    evidence: tuple[SearchEvidence, ...]
    confirmed_claims: tuple[EvidenceClaim, ...]
    contradictions: tuple[str, ...]
    unknowns: tuple[str, ...]
    refined_impact: str
    confidence: float
    stop_reason: str
    calls: int = 0
    latency_ms: int = 0


@dataclass(frozen=True)
class EventRecord:
    id: str
    title: str
    summary: str
    signal_ids: tuple[str, ...]
    material_ids: tuple[str, ...]
    compact_text: str
    embedding: tuple[float, ...] = ()
    version: int = 1
    lifecycle_state: Literal["active", "archived"] = "active"
    first_published_at: str | None = None
    last_published_at: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    last_meaningful_update_at: str | None = None
    archived_at: str | None = None


@dataclass(frozen=True)
class LinkDecision:
    signal_id: str
    event_id: str | None
    relation: Literal["same_event", "event_update", "related_topic", "different"]
    confidence: float
    evidence: str
    needs_human_review: bool = False


@dataclass(frozen=True)
class CriticResult:
    signal_id: str
    issues: tuple[str, ...]
    corrected_signal: SignalDraft
    needs_human_review: bool
    model: str
    latency_ms: int


@dataclass(frozen=True)
class DraftDigest:
    """Machine-produced release candidate before any human approval.

    Dictionaries are used at this boundary because the web/API and benchmark
    contracts intentionally expose the same JSON-compatible representation.
    """

    item_decisions: tuple[dict[str, Any], ...]
    objects: tuple[dict[str, Any], ...]
    deliveries: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class RuntimeTelemetry:
    configuration_id: str
    model: str
    context_version: str
    documents: int
    signals: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    provider_latency_ms: int
    wall_seconds: float
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentRunResult:
    """Stable output of the automatic product contour."""

    draft_digest: DraftDigest
    analyses: tuple[AnalysisDraft, ...]
    research: tuple[ResearchReport, ...]
    critics: tuple[CriticResult, ...]
    links: tuple[LinkDecision, ...]
    telemetry: RuntimeTelemetry
