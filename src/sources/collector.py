"""Collector: polls every enabled source through its adapter and persists what is new.

Workers do HTTP and parsing only; all SQLite writes happen on the main thread,
one transaction per source, so validators and cursors are never saved without
the documents they belong to. Failures are recorded on `fetch_state`, never
raised — one dead site must not stop the run.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import urlsplit

import httpx

from ..common import get_logger, parse_datetime, to_utc_iso, utc_now
from ..config import Config
from ..models import CollectReport, FetchResult, FetchState, RawDocument, Source
from ..paths import ProjectPaths
from ..storage import Database
from .base import HostLimiter, build_adapters, make_client
from .fulltext import FullTextFetcher

log = get_logger("collector")


def _placeholder_name(source: Source) -> bool:
    handle = urlsplit(source.fetch_url).path.rstrip("/").rsplit("/", 1)[-1]
    return source.name in ("", source.url, source.fetch_url, handle)


class Collector:
    def __init__(
        self,
        config: Config,
        paths: ProjectPaths,
        db: Database,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] = utc_now,
    ):
        self.config = config
        self.paths = paths
        self.db = db
        self.transport = transport
        self.now = now
        self.limiter = HostLimiter(config.scraper.per_host_concurrency)
        self.adapters = build_adapters(config, self.limiter)
        self.fulltext = FullTextFetcher(config.scraper, self.limiter)

    # ── run ────────────────────────────────────────────────────────────────

    def run(
        self, source_ids: list[int] | None = None, backfill: bool = False, force: bool = False
    ) -> CollectReport:
        started = self.now()
        if source_ids:
            sources = [s for s in self.db.sources.list() if s.id in set(source_ids)]
        else:
            sources = self.db.sources.list(enabled_only=True)
        sources = [s for s in sources if s.kind in self.adapters]
        if force:
            with self.db.transaction():
                for s in sources:
                    self.db.fetch_state.reset(s.id)
        states = {s.id: self.db.fetch_state.get(s.id) for s in sources}
        since = (
            None if backfill else started - timedelta(hours=self.config.scraper.date_window_hours)
        )
        report = CollectReport(started_at=to_utc_iso(started) or "")
        log.info(
            "collect: %d sources (window %sh%s)",
            len(sources),
            self.config.scraper.date_window_hours,
            ", backfill" if backfill else "",
        )

        with make_client(self.config, self.transport) as client:
            workers = min(self.config.scraper.concurrency, len(sources) or 1)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._poll, s, states[s.id], client, started, since, backfill): s
                    for s in sources
                }
                for fut in as_completed(futures):
                    source = futures[fut]
                    result, latency_ms = fut.result()
                    entry = self._persist(
                        source,
                        states[source.id],
                        result,
                        client,
                        started,
                        since,
                        backfill,
                        latency_ms,
                    )
                    report.per_source.append(entry)
                    status = entry["status"]
                    if status == "ok":
                        report.sources_ok += 1
                        report.docs_new += entry["new"]
                    elif status == "not_modified":
                        report.sources_not_modified += 1
                    else:
                        report.sources_fail += 1

        report.finished_at = to_utc_iso(self.now()) or ""
        self.db.runs.add(report)
        log.info("collect done: %s", report.summary_line())
        return report

    def _poll(
        self,
        source: Source,
        state: FetchState,
        client: httpx.Client,
        now: datetime,
        since: datetime | None,
        backfill: bool,
    ) -> tuple[FetchResult, float]:
        t0 = time.monotonic()
        try:
            result = self.adapters[source.kind].fetch(
                source, state, client, now=now, since=since, backfill=backfill
            )
        except Exception as e:  # noqa: BLE001 — a parser bug on one site must not kill the run
            log.exception("%s: adapter crashed", source.name)
            result = FetchResult(error=f"{e.__class__.__name__}: {e}")
        return result, (time.monotonic() - t0) * 1000.0

    # ── persistence (main thread) ──────────────────────────────────────────

    def _persist(
        self,
        source: Source,
        state: FetchState,
        result: FetchResult,
        client: httpx.Client,
        now: datetime,
        since: datetime | None,
        backfill: bool,
        latency_ms: float,
    ) -> dict:
        now_iso = to_utc_iso(now) or ""
        state.last_fetch_at = now_iso
        entry = {
            "id": source.id,
            "name": source.name,
            "kind": source.kind,
            "latency_ms": round(latency_ms),
            "new": 0,
            "seen": 0,
        }

        if result.error:
            state.last_error = result.error
            state.consecutive_failures += 1
            with self.db.transaction():
                self.db.fetch_state.save(state)
            log.warning("✗ %s: %s", source.name, result.error)
            return {**entry, "status": "failed", "error": result.error}

        if result.not_modified:
            state.last_error = None
            state.consecutive_failures = 0
            state.last_success_at = now_iso
            state.last_doc_count = 0
            with self.db.transaction():
                self.db.fetch_state.save(state)
            log.info("= %s: not modified (%.0f ms)", source.name, latency_ms)
            return {**entry, "status": "not_modified"}

        first_run = state.first_run
        candidates = result.documents
        entry["seen"] = len(candidates)
        docs = self._drop_known(source, candidates)
        seen_urls: list[str] = []
        if source.kind == "html":
            seen_urls = [d.url for d in candidates]
            known = self.db.seen_urls.known(source.id, seen_urls)
            docs = [d for d in docs if d.url not in known]
        docs = self._window_filter(docs, since)
        docs = docs[: self.config.scraper.max_new_per_source]

        if self.config.scraper.fetch_fulltext and any(d.needs_fulltext for d in docs):
            self.fulltext.enrich(client, docs)
        if source.kind in ("html", "sitemap") and first_run and since is not None:
            # First look at a list page / undated sitemap entries: only keep what we
            # can date inside the window, otherwise the whole site menu becomes "news".
            docs = [d for d in docs if d.published_at and parse_datetime(d.published_at) >= since]
        docs = [d for d in docs if d.title or d.text or d.summary]
        for d in docs:
            self._finalize(d, now)

        inserted = 0
        with self.db.transaction():
            for d in docs:
                if self.db.documents.exists(source.id, d.external_id):
                    continue
                self.db.documents.insert(d)
                inserted += 1
            if "etag" in result.state_update:
                state.etag = result.state_update["etag"]
            if "last_modified" in result.state_update:
                state.last_modified = result.state_update["last_modified"]
            if "cursor" in result.state_update:
                state.cursor = dict(result.state_update["cursor"] or {})
            state.last_error = None
            state.consecutive_failures = 0
            state.last_success_at = now_iso
            state.last_doc_count = inserted
            if seen_urls:
                self.db.seen_urls.add(source.id, seen_urls, now_iso)
            self.db.fetch_state.save(state)

        if result.source_title and _placeholder_name(source):
            source.name = result.source_title[:80]
            self.db.sources.update(source)
            entry["name"] = source.name

        log.info(
            "→ %s: %d entries (%d new, %.0f ms)", source.name, len(candidates), inserted, latency_ms
        )
        return {**entry, "status": "ok", "new": inserted}

    def _drop_known(self, source: Source, docs: list[RawDocument]) -> list[RawDocument]:
        """Skip documents already stored, by (source, external_id) or by URL.

        The URL rule is skipped for section/home URLs: some regulator feeds link
        every item to the same landing page.
        """
        own = {source.url.rstrip("/"), source.fetch_url.rstrip("/")}
        out: list[RawDocument] = []
        for d in docs:
            if self.db.documents.exists(source.id, d.external_id):
                continue
            url = d.url.rstrip("/")
            has_path = bool(urlsplit(d.url).path.strip("/"))
            if has_path and url not in own and self.db.documents.find_by_url(d.url):
                continue
            out.append(d)
        return out

    @staticmethod
    def _window_filter(docs: list[RawDocument], since: datetime | None) -> list[RawDocument]:
        """Drop documents dated before `since`; undated ones stay (НПА pages often lack a date)."""
        if since is None:
            return docs
        kept: list[RawDocument] = []
        for d in docs:
            dt = parse_datetime(d.published_at)
            if dt is not None and dt < since:
                continue
            kept.append(d)
        return kept

    @staticmethod
    def _finalize(doc: RawDocument, now: datetime) -> None:
        dt = parse_datetime(doc.published_at)
        if dt is not None and dt > now:
            doc.published_at = to_utc_iso(now)  # clock skew / typo'd future dates
        doc.needs_fulltext = False
        doc.compute_hash()

    # ── one-off import ─────────────────────────────────────────────────────

    def import_url(self, url: str) -> tuple[int, bool]:
        """Fetch one page into the built-in manual source. Returns (doc_id, created)."""
        manual = self.db.sources.ensure_manual()
        existing = self.db.documents.find_by_url(url)
        if existing is not None:
            return existing, False
        now = self.now()
        doc = RawDocument(
            source_id=manual.id or 0,
            external_id=url,
            url=url,
            fetched_at=to_utc_iso(now) or "",
            needs_fulltext=True,
        )
        with make_client(self.config, self.transport) as client:
            self.fulltext.enrich(client, [doc])
        if not doc.text and not doc.title:
            raise ValueError(f"could not extract anything readable from {url}")
        self._finalize(doc, now)
        with self.db.transaction():
            doc_id = self.db.documents.insert(doc)
        log.info("imported %s as #%d (%d chars)", url, doc_id, len(doc.text))
        return doc_id, True
