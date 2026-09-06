"""One headless vertical run through the approved AI contour."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Literal

from .analysis import PrimaryAnalyzer
from .contracts import (
    AnalysisDraft,
    CriticResult,
    EventRecord,
    GsLabsContext,
    LinkDecision,
    PreparedDocument,
    ResearchReport,
    SignalDraft,
)
from .critic import SignalCritic
from .events import EventLinker
from .research import TargetedResearcher
from .store import ProductStore


@dataclass(frozen=True)
class BranchConfiguration:
    a1: Literal["one_pass", "two_pass"] = "one_pass"
    a2: Literal["without_research", "targeted_research"] = "without_research"
    a3: Literal["full_scan", "embedding_top20"] = "full_scan"
    a4: Literal["without_critic", "with_critic"] = "without_critic"

    @property
    def id(self) -> str:
        return f"A1={self.a1};A2={self.a2};A3={self.a3};A4={self.a4}"


@dataclass(frozen=True)
class WorkflowResult:
    configuration_id: str
    analysis: AnalysisDraft
    research: tuple[ResearchReport, ...]
    links: tuple[LinkDecision, ...]
    critics: tuple[CriticResult, ...]
    npa_signal_ids: tuple[str, ...]
    queue_signal_ids: tuple[str, ...]


class ProductWorkflow:
    def __init__(
        self,
        analyzer: PrimaryAnalyzer,
        linker: EventLinker,
        store: ProductStore,
        *,
        researcher: TargetedResearcher | None = None,
        critic: SignalCritic | None = None,
    ):
        self.analyzer = analyzer
        self.linker = linker
        self.researcher = researcher
        self.critic = critic
        self.store = store

    def run(
        self,
        document: PreparedDocument,
        context: GsLabsContext,
        events: list[EventRecord],
        config: BranchConfiguration,
        *,
        raw_document_id: int | None = None,
    ) -> WorkflowResult:
        prepared_version = self.store.save_prepared(
            document.id, document, raw_document_id=raw_document_id
        )
        analysis = self.analyzer.analyze(document, context, mode=config.a1)
        analysis = replace(analysis, configuration_id=config.id)
        self.store.save_analysis(analysis, prepared_version=prepared_version)
        research_reports: list[ResearchReport] = []
        links: list[LinkDecision] = []
        critics: list[CriticResult] = []
        npa_signal_ids: list[str] = []
        queue: list[str] = []
        event_bank = list(events)

        for original_signal in analysis.signals:
            signal = original_signal
            research = self._research(signal, context, config)
            research_reports.append(research)
            self.store.save_research(research)

            if research.status in {"complete", "incomplete"}:
                researched = replace(
                    signal,
                    impact=research.refined_impact or signal.impact,
                    confidence=research.confidence,
                    unknowns=research.unknowns,
                )
                if researched != signal:
                    signal = researched
                    self.store.save_signal(
                        signal, actor="ai_research", reason="A2 evidence-based refinement"
                    )

            reviewed = self._critic(document, signal, config)
            critics.append(reviewed)
            if reviewed.corrected_signal != signal:
                signal = reviewed.corrected_signal
                self.store.save_signal(signal, actor="ai_critic", reason="A4 critic correction")

            if (
                signal.kind == "npa"
                and signal.npa_identifier
                and document.source_class in {"regulator", "npa"}
            ):
                self._upsert_npa(signal, document)
                npa_signal_ids.append(signal.signal_id)
            elif signal.kind in {"npa", "npa_candidate"}:
                self._save_npa_candidate(signal)
                npa_signal_ids.append(signal.signal_id)
            else:
                decision = self.linker.link(signal, event_bank, mode=config.a3)
                links.append(decision)
                self.store.save_link(decision)
                if decision.event_id and decision.relation in {"same_event", "event_update"}:
                    event_bank = self._append_to_event(
                        event_bank, decision.event_id, signal
                    )
                else:
                    event = self._new_event(signal)
                    self.store.save_event(event)
                    event_bank.append(event)
            queue.append(signal.signal_id)

        return WorkflowResult(
            config.id,
            analysis,
            tuple(research_reports),
            tuple(links),
            tuple(critics),
            tuple(npa_signal_ids),
            tuple(queue),
        )

    def _research(self, signal: SignalDraft, context: GsLabsContext, config: BranchConfiguration) -> ResearchReport:
        enabled = config.a2 == "targeted_research"
        if enabled and self.researcher is None:
            raise ValueError("A2 targeted_research selected without researcher")
        if self.researcher:
            return self.researcher.research(signal, context, enabled=enabled)
        from .research import TargetedResearcher
        return TargetedResearcher._empty(signal, "not_needed", "none", "A2 disabled")

    def _critic(self, document: PreparedDocument, signal: SignalDraft, config: BranchConfiguration) -> CriticResult:
        enabled = config.a4 == "with_critic"
        if enabled and self.critic is None:
            raise ValueError("A4 with_critic selected without critic")
        if self.critic:
            return self.critic.review(document, signal, enabled=enabled)
        return CriticResult(signal.signal_id, (), signal, False, self.analyzer.model, 0)

    def _upsert_npa(self, signal: SignalDraft, document: PreparedDocument) -> None:
        identifier = signal.npa_identifier or ""
        npa_id = hashlib.sha256(f"RU:{identifier}".encode()).hexdigest()[:16]
        row = self.store.conn.execute(
            "SELECT id FROM npa_records WHERE jurisdiction=? AND official_identifier=?",
            ("RU", identifier),
        ).fetchone()
        if row is None:
            self.store.conn.execute(
                "INSERT INTO npa_records(id,jurisdiction,official_identifier,official_url,created_at) VALUES(?,?,?,?,datetime('now'))",
                (npa_id, "RU", identifier, document.source_url),
            )
        else:
            npa_id = str(row["id"])
        current = self.store.conn.execute(
            "SELECT COALESCE(MAX(version),0), payload FROM npa_versions WHERE npa_id=?",
            (npa_id,),
        ).fetchone()
        version = int(current[0]) + 1
        payload = {"signal_id": signal.signal_id, "summary": signal.summary, "claims": [x.__dict__ for x in signal.claims]}
        import json
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        previous = self.store.conn.execute(
            "SELECT payload,stage FROM npa_versions WHERE npa_id=? ORDER BY version DESC LIMIT 1", (npa_id,)
        ).fetchone()
        if previous and previous["payload"] == encoded and previous["stage"] == signal.npa_stage:
            self.store.conn.commit()
            return
        self.store.conn.execute(
            "INSERT INTO npa_versions(npa_id,version,stage,payload,source_url,created_at) VALUES(?,?,?,?,?,datetime('now'))",
            (npa_id, version, signal.npa_stage, encoded, document.source_url or ""),
        )
        self.store.audit("npa.version_created", "npa", npa_id, "ai", {"version": version}, commit=False)
        self.store.conn.commit()

    def _save_npa_candidate(self, signal: SignalDraft) -> None:
        import json
        self.store.conn.execute(
            "INSERT INTO npa_candidates(signal_id,payload,created_at) VALUES(?,?,datetime('now'))",
            (signal.signal_id, json.dumps({"summary": signal.summary, "stage": signal.npa_stage}, ensure_ascii=False)),
        )
        self.store.audit("npa.candidate_created", "signal", signal.signal_id, "ai", commit=False)
        self.store.conn.commit()

    @staticmethod
    def _new_event(signal: SignalDraft) -> EventRecord:
        event_id = "evt-" + hashlib.sha256(signal.signal_id.encode()).hexdigest()[:12]
        return EventRecord(
            event_id,
            signal.summary,
            signal.summary,
            (signal.signal_id,),
            (signal.material_id,),
            f"{signal.summary}\n{' '.join(x.text for x in signal.claims)}\n{signal.impact}",
        )

    def _append_to_event(
        self, events: list[EventRecord], event_id: str, signal: SignalDraft
    ) -> list[EventRecord]:
        updated_bank: list[EventRecord] = []
        found = False
        for event in events:
            if event.id != event_id:
                updated_bank.append(event)
                continue
            found = True
            updated = replace(
                event,
                signal_ids=tuple(dict.fromkeys((*event.signal_ids, signal.signal_id))),
                material_ids=tuple(dict.fromkeys((*event.material_ids, signal.material_id))),
                compact_text=(
                    f"{event.compact_text}\n{signal.summary}\n"
                    f"{' '.join(x.text for x in signal.claims)}\n{signal.impact}"
                ).strip(),
                version=event.version + 1,
                embedding=(),
            )
            self.store.save_event(updated)
            updated_bank.append(updated)
        if not found:
            raise ValueError(f"linker returned unavailable event: {event_id}")
        return updated_bank
