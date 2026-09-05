"""Collector: polls every enabled source through its adapter and persists what is new.

Workers do HTTP and parsing only; all SQLite writes happen on the main thread,
one transaction per source, so validators and cursors are never saved without
the documents they belong to. Failures are recorded on `fetch_state`, never
raised — one dead site must not stop the run.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import urlsplit

import httpx

from ..common import get_logger, load_env_secret, parse_datetime, to_utc_iso, utc_now
from ..config import Config
from ..models import CollectReport, FetchResult, FetchState, RawDocument, Source
from ..paths import ProjectPaths
from ..storage import Database
from . import scheduler
from .base import HostLimiter, build_adapters, make_client
from .fulltext import FullTextFetcher
from .telegram_mtproto import MtprotoReader, credentials_from_env, reader_factory

log = get_logger("collector")

# После стольких неудач подряд источник помечается сломанным (spec 1.4, US-4).
FAILURES_TO_ERROR = 5


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
        tavily_key: str | None = None,
        mtproto_factory: Callable[[], MtprotoReader] | None = None,
    ):
        self.config = config
        self.paths = paths
        self.db = db
        self.transport = transport
        self.now = now
        self.limiter = HostLimiter(config.scraper.per_host_concurrency)
        if tavily_key is None:
            tavily_key = load_env_secret(config.tavily.api_key_env, paths.env_path)
        if mtproto_factory is None and config.telegram.mtproto != "off":
            creds = credentials_from_env(config.telegram, paths)
            # No session file means no MTProto: skip the doomed connect and let
            # telegram sources go straight to the t.me/s/ preview.
            if creds and os.path.exists(creds.session_path):
                mtproto_factory = reader_factory(creds, config.telegram)
        self.adapters = build_adapters(
            config, self.limiter, tavily_key=tavily_key, mtproto_factory=mtproto_factory
        )
        self.fulltext = FullTextFetcher(config.scraper, self.limiter)

    # ── run ────────────────────────────────────────────────────────────────

    def run(
        self,
        source_ids: list[int] | None = None,
        backfill: bool = False,
        force: bool = False,
        due_only: bool = False,
    ) -> CollectReport:
        started = self.now()
        if source_ids:
            sources = [s for s in self.db.sources.list() if s.id in set(source_ids)]
        elif due_only:
            # `collect --watch` тикает часто и почти всегда вхолостую: расписание
            # источника решает, чья очередь (scheduler.py).
            sources = scheduler.due(self.db, now=started)
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

        try:
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
                        self._tally(report, entry)
                        scheduler.reschedule(self.db, source, now=self.now())
        finally:
            self.close()
        report.finished_at = to_utc_iso(self.now()) or ""
        self.db.runs.add(report)
        log.info("collect done: %s", report.summary_line())
        return report

    def collect_one(self, source: Source, *, force: bool = False) -> tuple[FetchResult, dict]:
        """Poll one source right now, on the calling thread, and record the run.

        Returns the adapter's result (every hit, stored or not) alongside the
        report entry, so a caller can show what just came in without querying
        the source a second time. `force` clears validators and cursors first.
        """
        if source.id is None:
            raise ValueError("source must be stored before it can be collected")
        started = self.now()
        if force:
            with self.db.transaction():
                self.db.fetch_state.reset(source.id)
        state = self.db.fetch_state.get(source.id)
        since = started - timedelta(hours=self.config.scraper.date_window_hours)
        report = CollectReport(started_at=to_utc_iso(started) or "")
        try:
            if source.kind not in self.adapters:
                result = FetchResult(error=f"no adapter for kind '{source.kind}'")
                entry = self._persist(source, state, result, None, started, since, False, 0.0)
            else:
                with make_client(self.config, self.transport) as client:
                    result, latency_ms = self._poll(source, state, client, started, since, False)
                    entry = self._persist(
                        source, state, result, client, started, since, False, latency_ms
                    )
        finally:
            self.close()
        self._tally(report, entry)
        report.finished_at = to_utc_iso(self.now()) or ""
        self.db.runs.add(report)
        return result, entry

    def close(self) -> None:
        """Release what adapters hold open between runs (the MTProto client)."""
        for adapter in self.adapters.values():
            closer = getattr(adapter, "close", None)
            if closer is None:
                continue
            try:
                closer()
            except Exception as e:  # closing must never fail a completed run
                log.debug("closing the %s adapter: %s", adapter.kind, e)

    @staticmethod
    def _tally(report: CollectReport, entry: dict) -> None:
        report.per_source.append(entry)
        status = entry["status"]
        if status == "ok":
            report.sources_ok += 1
            report.docs_new += entry["new"]
        elif status == "not_modified":
            report.sources_not_modified += 1
        else:
            report.sources_fail += 1

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

    def _persist(self, source: Source, *args, **kwargs) -> dict:
        """Обёртка над записью результата: каждый опрос попадает в `source_runs`.

        История нужна US-4 — по одному «последнему состоянию» нельзя отличить
        источник, который молчит, от источника, который сломался неделю назад.
        """
        started = self.now()
        entry = self._persist_result(source, *args, **kwargs)
        try:
            run_id = self.db.source_runs.start(source.id, to_utc_iso(started) or "")
            error = entry.get("error") or ""
            self.db.source_runs.finish(
                run_id,
                items_found=int(entry.get("seen") or 0),
                items_new=int(entry.get("new") or 0),
                error_code=_error_code(error) if error else "",
                error_message=error,
            )
        except Exception as e:  # история не должна ронять сбор
            log.debug("не записана история опроса #%s: %s", source.id, e)
        return entry

    def _persist_result(
        self,
        source: Source,
        state: FetchState,
        result: FetchResult,
        client: httpx.Client | None,
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
                # Молчащий источник и сломанный различаются только так: после порога
                # он виден в списке как `error`, а не как обычный активный (US-4).
                if state.consecutive_failures >= FAILURES_TO_ERROR and source.status == "active":
                    self.db.sources.set_status(source.id, "error")
            log.warning("✗ %s: %s", source.name, result.error)
            return {**entry, "status": "failed", "error": result.error}

        if result.not_modified:
            state.last_error = None
            if source.status == "error":
                self.db.sources.set_status(source.id, "active")
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
        # A search source bounds its own recency (`days`), which may be wider than
        # the collect window; everything else is cut to the window here.
        docs = self._window_filter(docs, None if source.kind == "search" else since)
        # Cap the batch, but never at the expense of adapter-generated documents
        # without an origin URL (the search digest rides at the end of its batch).
        cap = self.config.scraper.max_new_per_source
        docs = docs[:cap] + [d for d in docs[cap:] if not d.url]

        if (
            client is not None
            and self.config.scraper.fetch_fulltext
            and any(d.needs_fulltext for d in docs)
        ):
            self.fulltext.enrich(client, docs)
        if source.kind in ("html", "sitemap") and first_run and since is not None:
            # First look at a list page / undated sitemap entries: only keep what we
            # can date inside the window, otherwise the whole site menu becomes "news".
            docs = [d for d in docs if d.published_at and parse_datetime(d.published_at) >= since]
        docs = [d for d in docs if d.title or d.text or d.summary]
        for d in docs:
            self._finalize(d, now)

        inserted: list[str] = []
        with self.db.transaction():
            for d in docs:
                if self.db.documents.exists(source.id, d.external_id):
                    continue
                self.db.documents.insert(d)
                inserted.append(d.external_id)
            if "etag" in result.state_update:
                state.etag = result.state_update["etag"]
            if "last_modified" in result.state_update:
                state.last_modified = result.state_update["last_modified"]
            if "cursor" in result.state_update:
                state.cursor = dict(result.state_update["cursor"] or {})
            state.last_error = None
            state.consecutive_failures = 0
            state.last_success_at = now_iso
            state.last_doc_count = len(inserted)
            if source.status == "error":
                self.db.sources.set_status(source.id, "active")  # починился сам
            if seen_urls:
                self.db.seen_urls.add(source.id, seen_urls, now_iso)
            self.db.fetch_state.save(state)

        if result.source_title and _placeholder_name(source):
            source.name = result.source_title[:80]
            self.db.sources.update(source)
            entry["name"] = source.name

        log.info(
            "→ %s: %d entries (%d new, %.0f ms)",
            source.name,
            len(candidates),
            len(inserted),
            latency_ms,
        )
        return {**entry, "status": "ok", "new": len(inserted), "new_external_ids": inserted}

    def _drop_known(self, source: Source, docs: list[RawDocument]) -> list[RawDocument]:
        """Skip documents already stored, by (source, external_id) or by URL.

        The URL rule is skipped for section/home URLs and for documents without
        one (search digests): some regulator feeds link every item to the same
        landing page.
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


# Словарь для UI: по коду видно, чинить сеть, парсер или доступ (US-4).
_ERROR_CODES = (
    ("timeout", "timeout"),
    ("timed out", "timeout"),
    ("не отвечает", "timeout"),
    ("HTTP 4", "http_error"),
    ("HTTP 5", "http_error"),
    ("preview unavailable", "telegram_preview_unavailable"),
    ("MTProto", "telegram_preview_unavailable"),
    ("certificate", "network_unreachable"),
    ("connect", "network_unreachable"),
    ("DNS", "network_unreachable"),
    ("parse", "parse_error"),
    ("no adapter", "parse_error"),
)


def _error_code(error: str) -> str:
    low = (error or "").lower()
    for needle, code in _ERROR_CODES:
        if needle.lower() in low:
            return code
    return "error"
