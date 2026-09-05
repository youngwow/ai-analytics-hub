"""ProcessingService — the one entry point for stage 1.2.

The CLI is a thin wrapper over these methods and the future REST layer will be
another one (constitution, principle III): no business logic lives above this
file.

Shape of a run, mirroring the collector's split of duties: workers do the slow
model calls, the main thread owns every write, one transaction per cluster.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta

from ..common import get_logger, to_utc_iso, utc_now
from ..config import Config
from ..models import (
    Cluster,
    CompanyProfile,
    EntitySpan,
    Item,
    ItemRevision,
    LlmCall,
    NpaEvent,
    RawDocument,
)
from ..storage import Database
from . import dedup, normalize, prompts
from . import profile as profile_mod
from .llm import EmbeddingProvider, LlmError, LLMProvider
from .pipeline import Draft, Pipeline

log = get_logger("processing")

EDITABLE_FIELDS = ("title", "summary", "priority", "type", "tags", "npa_status", "analyst_note")
# A later publication may only move a bill forward; a retrospective article must
# not drag a card back to "анонс".
_STATUS_ORDER = ("анонс", "разработка", "внесён", "рассмотрение", "принят", "действует", "архив")


@dataclass
class ProcessingReport:
    documents: int = 0
    clusters: int = 0
    items_new: int = 0
    items_joined: int = 0
    items_updated: int = 0
    degraded: int = 0
    needs_review: int = 0
    calls: int = 0
    failed: int = 0
    elapsed_s: float = 0.0
    latencies_ms: list[int] = field(default_factory=list)

    @property
    def avg_latency_ms(self) -> int:
        return int(sum(self.latencies_ms) / len(self.latencies_ms)) if self.latencies_ms else 0


@dataclass
class _Unit:
    """One prospective card: the canonical document plus everything joining it."""

    document_id: int
    document: RawDocument
    norm_text: str
    simhash: str
    embedding: list[float] = field(default_factory=list)
    members: list[int] = field(default_factory=list)
    join_item_id: int | None = None
    join_cluster_id: int | None = None
    divergent: bool = False
    draft: Draft | None = None


class ProcessingService:
    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        provider: LLMProvider | None = None,
        embedder: EmbeddingProvider | None = None,
    ):
        self.config = config
        self.db = db
        self.provider = provider
        self.embedder = embedder
        self.pipeline = Pipeline(config.processing, config.llm, provider)

    # -- reading --

    def list_items(self, **filters) -> list:
        return self.db.items.list(**filters)

    def get_item(self, item_id: int) -> dict | None:
        item = self.db.items.get(item_id)
        if item is None:
            return None
        return {
            "item": item,
            "entities": self.db.items.entities(item_id),
            "sources": self.db.items.sources(item_id),
            "events": self.db.items.events(item_id),
            "revisions": self.db.items.revisions(item_id),
        }

    # -- the run --

    def run(
        self,
        *,
        limit: int | None = None,
        source_id: int | None = None,
        since: str | None = None,
        profile_id: int | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> ProcessingReport:
        """Turn unprocessed documents into cards. Idempotent: a repeat run adds none."""
        started = time.monotonic()
        report = ProcessingReport()
        limit = limit or self.config.processing.max_new_per_run
        rows = self.db.documents.unprocessed(
            limit=limit, source_id=source_id, since=since, force=force
        )
        report.documents = len(rows)
        if not rows:
            report.elapsed_s = time.monotonic() - started
            return report

        # `--force` means "read these documents again", not "make a second card for
        # them": anything already carded goes through reprocess, which respects the
        # analyst's edits.
        carded: list[int] = []
        if force:
            fresh_rows = []
            for row in rows:
                item_id = self.db.items.item_for_document(int(row["id"]))
                (carded.append(item_id) if item_id else fresh_rows.append(row))
            rows = fresh_rows

        units = self._prepare(rows, embed=not dry_run) if rows else []
        report.clusters = len(units)
        if dry_run:
            report.items_updated = len(carded)
            report.elapsed_s = time.monotonic() - started
            return report

        company = profile_mod.resolve(self.db, profile_id)
        prompt_version = self.db.prompts.ensure(
            prompts.STAGE,
            prompts.SYSTEM,
            self.config.llm.model,
            {"temperature": self.config.llm.temperature},
        )

        fresh = [u for u in units if u.join_item_id is None]
        self._draft_all(fresh, company)
        for unit in units:
            try:
                if unit.join_item_id is not None:
                    self._join(unit)
                    report.items_joined += 1
                else:
                    self._store(unit, company, prompt_version, report)
            except LlmError:  # a configuration failure must stop the run loudly
                raise
            except Exception as e:  # one bad document must not lose the whole batch
                report.failed += 1
                log.error("документ #%s не обработан: %s", unit.document_id, e)

        for item_id in dict.fromkeys(carded):
            try:
                self.reprocess(item_id, profile_id=profile_id)
                report.items_updated += 1
            except (ValueError, LookupError) as e:
                report.failed += 1
                log.error("карточка #%s не пересобрана: %s", item_id, e)
        report.elapsed_s = time.monotonic() - started
        return report

    def _prepare(self, rows, embed: bool = True) -> list[_Unit]:
        """S0 + S1 on the main thread: normalise, hash, embed, group.

        `--dry-run` must not touch the provider at all, embeddings included, so a
        plan can be printed with no key and no spend.
        """
        units: list[_Unit] = []
        window = self._window_start()
        candidates = self.db.documents.clustered_candidates(since=window)
        texts: list[str] = []
        pending: list[_Unit] = []

        for row in rows:
            document = RawDocument.from_row(row)
            body = document.text or document.summary or document.title
            norm = normalize.normalize(body)
            unit = _Unit(
                document_id=int(row["id"]),
                document=document,
                norm_text=norm,
                simhash=dedup.simhash(norm),
                members=[int(row["id"])],
            )
            pending.append(unit)
            texts.append(normalize.clip(norm or document.title, 2000))

        vectors = self._embed(texts) if embed else []
        for unit, vector in zip(pending, vectors or [[]] * len(pending)):
            unit.embedding = vector
            match = dedup.find_match(
                candidates,
                text_simhash=unit.simhash,
                embedding=unit.embedding,
                max_distance=self.config.processing.simhash_distance,
                threshold=self.config.processing.cosine_threshold,
            )
            if match is not None:
                unit.join_item_id, unit.join_cluster_id = match.item_id, match.cluster_id
                unit.divergent = dedup.divergent(unit.norm_text, match_text(candidates, match))
                units.append(unit)
                continue
            sibling = self._sibling(units, unit)
            if sibling is not None:
                sibling.members.append(unit.document_id)
                sibling.divergent = sibling.divergent or dedup.divergent(
                    sibling.norm_text, unit.norm_text
                )
                if len(unit.norm_text) > len(sibling.norm_text):
                    # The fullest version is the canonical one (spec: «за канонический
                    # текст берётся самый полный / первоисточник»).
                    sibling.document_id, sibling.document = unit.document_id, unit.document
                    sibling.norm_text, sibling.simhash = unit.norm_text, unit.simhash
                    sibling.embedding = unit.embedding
                self._persist_derived(unit)
                continue
            units.append(unit)
        return units

    def _sibling(self, units: list[_Unit], unit: _Unit) -> _Unit | None:
        """A reprint of something already in this same batch."""
        for other in units:
            if other.join_item_id is not None:
                continue
            distance = dedup.hamming(unit.simhash, other.simhash)
            if distance <= self.config.processing.simhash_distance:
                return other
            if unit.embedding and other.embedding:
                score = dedup.cosine(unit.embedding, other.embedding)
                if score >= self.config.processing.cosine_threshold:
                    return other
        return None

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """One batched call; failure degrades S1 to URL + SimHash, never stops the run."""
        if not self.embedder or not texts:
            return []
        try:
            return self.embedder.embed(texts)
        except LlmError as e:
            log.warning("эмбеддинги недоступны (%s): кластеризация только по SimHash", e)
            return []

    def _draft_all(self, units: list[_Unit], company: CompanyProfile) -> None:
        """The slow part: one model call per cluster, bounded by processing.concurrency."""
        if not units:
            return
        workers = max(1, min(self.config.processing.concurrency, len(units)))

        def work(unit: _Unit) -> None:
            unit.draft = self.pipeline.process(
                unit.norm_text,
                title=unit.document.title,
                source=unit.document.author or unit.document.url,
                published=unit.document.published_at or "",
                profile=company,
            )

        if workers == 1:
            for unit in units:
                work(unit)
            return
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="llm") as pool:
            list(pool.map(work, units))

    def _store(
        self,
        unit: _Unit,
        company: CompanyProfile,
        prompt_version: int,
        report: ProcessingReport,
    ) -> None:
        """Write one card: cluster, item, entities, sources and telemetry, atomically."""
        draft = unit.draft
        if draft is None:
            return
        now = to_utc_iso(utc_now()) or ""
        with self.db.transaction():
            self._persist_derived(unit)
            existing = (
                self.db.items.by_npa_key(draft.npa_key)
                if draft.type == "npa" and draft.npa_key
                else None
            )
            if existing is not None:
                self._attach_to_npa(existing, unit, draft, now)
                self._record_calls(draft.calls, existing.id, report)
                report.items_joined += 1
            else:
                cluster_id = self.db.clusters.add(
                    Cluster(
                        canonical_document_id=unit.document_id,
                        centroid_embedding=dedup.encode_vector(unit.embedding)
                        if unit.embedding
                        else None,
                        size=len(unit.members),
                        has_divergent_opinions=unit.divergent,
                        created_at=now,
                    )
                )
                item = Item(
                    cluster_id=cluster_id,
                    type=draft.type,
                    npa_status=draft.npa_status,
                    npa_key=draft.npa_key,
                    title=unit.document.title or draft.title,
                    summary=draft.summary_text,
                    priority=draft.priority,
                    relevance_score=draft.relevance_score,
                    reasoning=draft.reasoning,
                    confidence=draft.confidence,
                    tags=draft.tags,
                    degraded=draft.degraded,
                    needs_review=draft.needs_review,
                    date_estimated=not unit.document.published_at,
                    model_name=draft.model_name,
                    prompt_version=prompt_version,
                    profile_version=company.version,
                    processed_at=now,
                    published_at=unit.document.published_at,
                )
                item_id = self.db.items.add(item)
                self.db.items.add_entities(item_id, self._entities(draft))
                self.db.items.link_sources(item_id, unit.members, unit.document_id)
                if draft.type == "npa" and draft.npa_status:
                    self.db.items.add_event(
                        NpaEvent(
                            item_id=item_id,
                            status=draft.npa_status,
                            occurred_at=unit.document.published_at,
                            source_url=unit.document.url,
                            created_by="system",
                            created_at=now,
                        )
                    )
                report.items_new += 1
                self._record_calls(draft.calls, item_id, report)
            if draft.degraded:
                report.degraded += 1
            if draft.needs_review:
                report.needs_review += 1

    def _attach_to_npa(self, item: Item, unit: _Unit, draft: Draft, now: str) -> None:
        """A new publication about a tracked act: extend the card, never duplicate it."""
        added = self.db.items.link_sources(item.id, unit.members)
        self.db.clusters.grow(item.cluster_id, added)
        status = draft.npa_status
        if status and _advances(item.npa_status, status):
            item.npa_status = status
            self.db.items.update(item)
        if status and not self.db.items.has_event(item.id, status):
            self.db.items.add_event(
                NpaEvent(
                    item_id=item.id,
                    status=status,
                    occurred_at=unit.document.published_at,
                    source_url=unit.document.url,
                    created_by="system",
                    created_at=now,
                )
            )

    def _join(self, unit: _Unit) -> None:
        """A reprint of an existing card: one more source, no model call."""
        with self.db.transaction():
            self._persist_derived(unit)
            added = self.db.items.link_sources(unit.join_item_id, unit.members)
            self.db.clusters.grow(unit.join_cluster_id, added, divergent=unit.divergent)

    def _persist_derived(self, unit: _Unit) -> None:
        self.db.documents.set_derived(
            unit.document_id,
            simhash=unit.simhash,
            embedding=dedup.encode_vector(unit.embedding) if unit.embedding else None,
            norm_text=unit.norm_text,
        )

    def _entities(self, draft: Draft) -> list[EntitySpan]:
        spans = [
            EntitySpan(role=role, value=value)
            for role, value in draft.entities.items()
            if value and role in ("who", "what", "when", "impact")
        ]
        if draft.npa_key:
            spans.append(
                EntitySpan(role="act_number", value=draft.npa_key, normalized_value=draft.npa_key)
            )
        return spans

    def _record_calls(self, calls: list[LlmCall], item_id: int, report: ProcessingReport) -> None:
        for call in calls:
            call.item_id = item_id
            self.db.llm_calls.add(call)
            report.calls += 1
            if call.status == "ok" and call.latency_ms:
                report.latencies_ms.append(call.latency_ms)

    def _window_start(self) -> str | None:
        days = self.config.processing.candidate_window_days
        return to_utc_iso(utc_now() - timedelta(days=days))

    # -- editing --

    def edit_item(self, item_id: int, fields: dict, actor: str = "user") -> Item:
        """Apply an analyst's edit: mark the field, keep the before/after in history."""
        item = self.db.items.get(item_id)
        if item is None:
            raise ValueError(f"карточка #{item_id} не найдена")
        changed: list[ItemRevision] = []
        for name, value in fields.items():
            if value is None or name not in EDITABLE_FIELDS:
                continue
            old = getattr(item, name)
            if old == value:
                continue
            changed.append(
                ItemRevision(
                    item_id=item_id,
                    field=name,
                    old_value=_as_text(old),
                    new_value=_as_text(value),
                    actor=actor,
                )
            )
            setattr(item, name, value)
            if name not in item.edited_fields:
                item.edited_fields.append(name)
        if not changed:
            return item
        with self.db.transaction():
            self.db.items.update(item)
            for revision in changed:
                self.db.items.add_revision(revision)
        return item

    def add_npa_event(
        self,
        item_id: int,
        status: str,
        *,
        occurred_at: str | None = None,
        source_url: str = "",
        note: str = "",
        actor: str = "user",
    ) -> NpaEvent:
        item = self.db.items.get(item_id)
        if item is None:
            raise ValueError(f"карточка #{item_id} не найдена")
        event = NpaEvent(
            item_id=item_id,
            status=status,
            occurred_at=occurred_at,
            source_url=source_url,
            note=note,
            created_by=actor,
        )
        with self.db.transaction():
            self.db.items.add_event(event)
            if _advances(item.npa_status, status) and "npa_status" not in item.edited_fields:
                item.npa_status = status
                self.db.items.update(item)
        return event

    def reprocess(
        self,
        item_id: int,
        *,
        stages: list[str] | None = None,
        keep_human_edits: bool = True,
        profile_id: int | None = None,
    ) -> Item:
        """Re-run the model for one card. Human edits win unless explicitly dropped."""
        item = self.db.items.get(item_id)
        if item is None:
            raise ValueError(f"карточка #{item_id} не найдена")
        sources = self.db.items.sources(item_id)
        canonical = next((s for s in sources if s["is_canonical"]), sources[0] if sources else None)
        if canonical is None:
            raise ValueError(f"у карточки #{item_id} нет исходных документов")
        row = self.db.documents.get(int(canonical["id"]))
        if row is None:
            raise ValueError("исходный документ удалён")

        company = profile_mod.resolve(self.db, profile_id)
        norm = normalize.normalize(row.text or row.summary or row.title)
        draft = self.pipeline.process(
            norm,
            title=row.title,
            source=row.author or row.url,
            published=row.published_at or "",
            profile=company,
        )
        wanted = set(stages or ["summary", "priority", "type", "tags", "entities"])
        protected = set(item.edited_fields) if keep_human_edits else set()
        now = to_utc_iso(utc_now()) or ""

        if "summary" in wanted and "summary" not in protected:
            item.summary = draft.summary_text
        if "priority" in wanted and "priority" not in protected:
            item.priority = draft.priority
            item.relevance_score = draft.relevance_score
            item.reasoning = draft.reasoning
        if "type" in wanted and "type" not in protected:
            item.type = draft.type
            item.npa_key = draft.npa_key
        if "tags" in wanted and "tags" not in protected:
            item.tags = draft.tags
        item.confidence = draft.confidence
        item.degraded = draft.degraded
        item.needs_review = draft.needs_review
        item.model_name = draft.model_name or item.model_name
        item.profile_version = company.version
        item.processed_at = now

        with self.db.transaction():
            self.db.items.update(item)
            if "entities" in wanted:
                self.db.items.clear_entities(item_id)
                self.db.items.add_entities(item_id, self._entities(draft))
            for call in draft.calls:
                call.item_id = item_id
                self.db.llm_calls.add(call)
        return item

    # -- quality --

    def quality_summary(self, since: str | None = None, until: str | None = None) -> dict:
        """What the database can honestly say about quality without a labelled set."""
        stats = self.db.llm_calls.stats(since, until)
        rows = self.db.items.list(limit=100000, include_hidden=True)
        total = len(rows)
        by_priority = {p: 0 for p in ("high", "medium", "low")}
        degraded = review = 0
        for row in rows:
            by_priority[row["priority"]] = by_priority.get(row["priority"], 0) + 1
            degraded += bool(row["degraded"])
            review += bool(row["needs_review"])
        return {
            "items": total,
            "by_priority": by_priority,
            "degraded": degraded,
            "hallucination_flags": review,
            "edited_share": round(self.db.items.edited_share(since), 3),
            "calls": stats["calls"],
            "avg_latency_ms": int(stats["avg_latency_ms"] or 0),
            "tokens_in": stats["tokens_in"],
            "tokens_out": stats["tokens_out"],
            "failed_calls": stats["failed"] or 0,
        }

    def close(self) -> None:
        closer = getattr(self.provider, "close", None)
        if callable(closer):
            closer()


def match_text(candidates, match) -> str:
    """The title of the card a document is joining — enough to spot a commentary."""
    for row in candidates:
        if row["item_id"] == match.item_id:
            return row["title"] or ""
    return ""


def _advances(current: str | None, candidate: str) -> bool:
    """True when `candidate` is a later stage than `current` — statuses never go back."""
    if candidate not in _STATUS_ORDER:
        return False
    if not current or current not in _STATUS_ORDER:
        return True
    return _STATUS_ORDER.index(candidate) > _STATUS_ORDER.index(current)


def _as_text(value) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(map(str, value))
    return "" if value is None else str(value)
