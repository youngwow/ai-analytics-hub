"""One production runtime from prepared materials to a reviewable digest.

Benchmarks may adapt their input into ``PreparedDocument`` objects, but they do
not get a private orchestration path. Strategy switches live in
``BranchConfiguration`` and no benchmark label is inspected here.
"""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from .analysis import PrimaryAnalyzer
from .contracts import (
    AgentRunResult,
    CriticResult,
    DraftDigest,
    EventRecord,
    GsLabsContext,
    LinkDecision,
    PreparedDocument,
    ResearchReport,
    RuntimeTelemetry,
    SignalDraft,
)
from .critic import SignalCritic
from .events import EventLinker
from .npa import NpaResolution, NpaResolver
from .release import build_deliveries, event_object, item_decision, npa_object
from .research import TargetedResearcher
from .store import ProductStore
from .workflow import BranchConfiguration


class ProductAgentRuntime:
    """Execute every automatic step while preserving review as a boundary."""

    def __init__(
        self,
        analyzer: PrimaryAnalyzer,
        linker: EventLinker,
        npa_resolver: NpaResolver,
        store: ProductStore,
        *,
        researcher: TargetedResearcher | None = None,
        critic: SignalCritic | None = None,
        analysis_concurrency: int = 1,
    ) -> None:
        self.analyzer = analyzer
        self.linker = linker
        self.npa_resolver = npa_resolver
        self.store = store
        self.researcher = researcher
        self.critic = critic
        if analysis_concurrency < 1:
            raise ValueError("analysis_concurrency must be >= 1")
        self.analysis_concurrency = analysis_concurrency

    def run(
        self,
        documents: list[PreparedDocument],
        context: GsLabsContext,
        config: BranchConfiguration,
        *,
        initial_state: dict[str, Any] | None = None,
        raw_document_ids: dict[str, int] | None = None,
        scheduled_release: bool = False,
    ) -> AgentRunResult:
        started = time.monotonic()
        initial_state = initial_state or {}
        raw_document_ids = raw_document_ids or {}
        self._validate_configuration(config)
        self.store.ensure_context(context.version, context, actor="runtime")

        analyses = []
        research_reports: list[ResearchReport] = []
        critic_results: list[CriticResult] = []
        errors: list[str] = []
        final_signals: dict[str, SignalDraft] = {}

        prepared_versions = {}
        for document in documents:
            prepared_version = self.store.save_prepared(
                document.id,
                document,
                raw_document_id=raw_document_ids.get(document.id),
            )
            prepared_versions[document.id] = prepared_version

        def analyze(document: PreparedDocument):
            return self.analyzer.analyze(document, context, mode=config.a1)

        if self.analysis_concurrency == 1:
            drafts = [analyze(document) for document in documents]
        else:
            with ThreadPoolExecutor(max_workers=self.analysis_concurrency) as executor:
                drafts = list(executor.map(analyze, documents))

        for document, draft in zip(documents, drafts):
            draft = replace(draft, configuration_id=config.id)
            self.store.save_analysis(
                draft, prepared_version=prepared_versions[document.id]
            )
            resolved_signals = []
            for original in draft.signals:
                signal = original
                if config.a2 == "targeted_research":
                    assert self.researcher is not None
                    report = self.researcher.research(signal, context, enabled=True)
                    research_reports.append(report)
                    self.store.save_research(report)
                    if report.status in {"complete", "incomplete"}:
                        updated = replace(
                            signal,
                            impact=report.refined_impact or signal.impact,
                            confidence=report.confidence,
                            unknowns=report.unknowns,
                        )
                        if updated != signal:
                            signal = updated
                            self.store.save_signal(
                                signal,
                                actor="ai_research",
                                reason="A2 evidence-based refinement",
                            )
                if config.a4 == "with_critic":
                    assert self.critic is not None
                    reviewed = self.critic.review(document, signal, enabled=True)
                    critic_results.append(reviewed)
                    if reviewed.corrected_signal != signal:
                        signal = reviewed.corrected_signal
                        self.store.save_signal(
                            signal,
                            actor="ai_critic",
                            reason="A4 critic correction",
                        )
                resolved_signals.append(signal)
                final_signals[signal.signal_id] = signal
            final_draft = replace(draft, signals=tuple(resolved_signals))
            analyses.append(final_draft)
            if final_draft.status == "failed":
                errors.append(f"{document.id}: {final_draft.reason}")

        document_map = {document.id: document for document in documents}
        npa_signals = [
            signal
            for signal in final_signals.values()
            if signal.kind in {"npa", "npa_candidate"}
        ]
        resolutions = self._resolve_npas(
            npa_signals, document_map, initial_state, mode=config.a3
        )
        resolved_npa_ids = {
            signal_id
            for resolution in resolutions
            for signal_id in resolution.member_signal_ids
        }
        event_signals = [
            signal
            for signal in final_signals.values()
            if signal.signal_id not in resolved_npa_ids
            and signal.relevance != "irrelevant"
        ]
        events, links = self._link_events(event_signals, initial_state, config)

        for event in events:
            if event.material_ids:
                self.store.save_event(event)
        # event_links has foreign keys to both signals and events. Persist the
        # final event projection first, then its decisions; otherwise links to
        # an initial-state or newly created event can fail mid-run.
        for link in links:
            self.store.save_link(link)
        for resolution in resolutions:
            payload = npa_object(resolution, final_signals)
            if payload is not None:
                self.store.save_npa_resolution(resolution, payload)

        initial_event_ids = {
            str(row["object_id"])
            for row in initial_state.get("known_events", [])
            if row.get("object_id")
        }
        objects = [
            event_object(
                event,
                final_signals,
                kind=(
                    "event"
                    if event.id in initial_event_ids or len(event.material_ids) > 1
                    else "publication"
                ),
            )
            for event in events
            if event.material_ids
        ]
        objects.extend(npa_object(resolution, final_signals) for resolution in resolutions)
        visible_objects = tuple(item for item in objects if item is not None)
        deliverable_ids = {
            obj["object_id"]
            for obj in visible_objects
            if any(
                signal.relevance == "relevant" or signal.critical_or_escalate
                for signal in final_signals.values()
                if signal.material_id in obj["member_ids"]
            )
        }
        digest = DraftDigest(
            item_decisions=tuple(
                item_decision(document.id, draft)
                for document, draft in zip(documents, analyses)
            ),
            objects=visible_objects,
            deliveries=tuple(
                build_deliveries(
                    visible_objects,
                    allowed_object_ids=deliverable_ids,
                    scheduled_release=scheduled_release,
                )
            ),
        )
        return AgentRunResult(
            draft_digest=digest,
            analyses=tuple(analyses),
            research=tuple(research_reports),
            critics=tuple(critic_results),
            links=tuple(links),
            telemetry=RuntimeTelemetry(
                configuration_id=config.id,
                model=self.analyzer.model,
                context_version=context.version,
                documents=len(documents),
                signals=len(final_signals),
                model_calls=sum(row.calls for row in analyses)
                + sum(row.calls for row in research_reports),
                input_tokens=sum(row.input_tokens for row in analyses),
                output_tokens=sum(row.output_tokens for row in analyses),
                provider_latency_ms=sum(row.latency_ms for row in analyses)
                + sum(row.latency_ms for row in research_reports)
                + sum(row.latency_ms for row in critic_results),
                wall_seconds=time.monotonic() - started,
                errors=tuple(errors),
            ),
        )

    def _validate_configuration(self, config: BranchConfiguration) -> None:
        if config.a2 == "targeted_research" and self.researcher is None:
            raise ValueError("A2 targeted_research selected without researcher")
        if config.a4 == "with_critic" and self.critic is None:
            raise ValueError("A4 with_critic selected without critic")

    def _resolve_npas(
        self,
        signals: list[SignalDraft],
        documents: dict[str, PreparedDocument],
        initial_state: dict[str, Any],
        *,
        mode: str,
    ) -> tuple[NpaResolution, ...]:
        tracked = list(initial_state.get("tracked_npas", []))
        resolved = self.npa_resolver.resolve(signals, documents, tracked, mode=mode)
        tracked_ids = {
            str(item["object_id"]) for item in tracked if item.get("object_id")
        }
        return tuple(
            row for row in resolved if row.external_id or row.object_id in tracked_ids
        )

    def _link_events(
        self,
        signals: list[SignalDraft],
        initial_state: dict[str, Any],
        config: BranchConfiguration,
    ) -> tuple[list[EventRecord], list[LinkDecision]]:
        events = [
            EventRecord(
                id=str(row["object_id"]),
                title=str(row["title"]),
                summary=str(row["title"]),
                signal_ids=(),
                material_ids=(),
                compact_text=str(row["title"]),
            )
            for row in initial_state.get("known_events", [])
            if row.get("status") != "archived"
        ]
        links = []
        for signal in signals:
            decision = self.linker.link(signal, events, mode=config.a3)
            links.append(decision)
            if decision.event_id and decision.relation in {"same_event", "event_update"}:
                events = [
                    replace(
                        event,
                        signal_ids=tuple(dict.fromkeys((*event.signal_ids, signal.signal_id))),
                        material_ids=tuple(dict.fromkeys((*event.material_ids, signal.material_id))),
                        compact_text=(
                            f"{event.compact_text}\n{signal.summary}\n{signal.impact}"
                        ).strip(),
                        embedding=(),
                        version=event.version + 1,
                    )
                    if event.id == decision.event_id
                    else event
                    for event in events
                ]
            else:
                event_id = "evt-" + hashlib.sha256(signal.signal_id.encode()).hexdigest()[:12]
                events.append(
                    EventRecord(
                        event_id,
                        signal.summary,
                        signal.summary,
                        (signal.signal_id,),
                        (signal.material_id,),
                        f"{signal.summary}\n{signal.impact}",
                    )
                )
        return events, links
