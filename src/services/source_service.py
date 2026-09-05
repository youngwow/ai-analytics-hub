"""SourceService — управление источниками (этап 1.4).

Сервис из Model-Service-Repository: принимает `Config` и `Database`, зовёт
репозитории, ошибки бросает подклассами `SourceError`.

Пользователь вставляет ссылку и не обязан знать, что такое RSS: `probe()` гоняет
её через тот же резолвер, что и `collect`, показывает превью и говорит, добавлен
ли уже такой источник. Ничего не удаляется физически — только `status`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..exceptions import (
    SourceExistsError,
    SourceNotFoundError,
    SourceValidationError,
    UnsupportedSourceError,
)
from ..models import POLL_INTERVALS, SOURCE_STATUSES, FetchState, Source, SourceRun
from ..paths import ProjectPaths
from ..repositories import Database
from ..sources import scheduler
from ..sources.base import HostLimiter, build_adapters, make_client
from ..sources.collector import Collector
from ..sources.resolver import Resolver
from ..sources.textutil import normalized_source_url
from ..utils import get_logger, to_utc_iso, utc_now

log = get_logger("sources")

PREVIEW_LIMIT = 5
# Хосты, где полный текст закрыт: честнее пометить источник, чем саммаризировать обрезок.
PAYWALL_HOSTS = ("vedomosti.ru", "kommersant.ru", "forbes.ru", "rbc.ru")
BOT_HOSTS = ("tadviser.ru",)
NO_PREVIEW_KINDS = ("manual", "search")



@dataclass
class ProbeResult:
    """Что система поняла про ссылку до того, как что-то сохранила."""

    resolved_type: str = "unsupported"
    feed_url: str = ""
    title: str = ""
    detection_method: str = "direct"
    suggested_poll_interval: str = scheduler.DEFAULT_INTERVAL
    already_exists: bool = False
    already_exists_source_id: int | None = None
    preview: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    note: str = ""


class SourceService:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    # -- разбор ссылки --

    def probe(self, url: str, *, with_preview: bool = True) -> ProbeResult:
        """Распознать ссылку и показать несколько материалов. В БД не пишет."""
        if not url.strip():
            raise SourceValidationError("пустая ссылка")
        limiter = HostLimiter(self.config.scraper.per_host_concurrency)
        with make_client(self.config) as client:
            resolution = Resolver(client, limiter).resolve(url)
            existing = self.db.sources.get_by_normalized(
                normalized_source_url(resolution.fetch_url or url)
            )
            result = ProbeResult(
                resolved_type=resolution.kind,
                feed_url=resolution.fetch_url,
                title=resolution.name,
                detection_method="direct" if resolution.fetch_url == url else "resolver",
                already_exists=existing is not None,
                already_exists_source_id=existing.id if existing else None,
                note=resolution.note,
            )
            result.suggested_poll_interval = scheduler.suggest_interval(
                _guess_category(url, resolution.kind)
            )
            if with_preview and resolution.kind not in NO_PREVIEW_KINDS:
                result.preview, preview_warnings = self._preview(client, limiter, resolution)
                result.warnings.extend(preview_warnings)
        result.warnings.extend(_host_warnings(resolution.fetch_url or url))
        if resolution.kind == "unsupported":
            result.warnings.append("unsupported_source")
        return result

    def _preview(self, client, limiter, resolution) -> tuple[list[dict], list[str]]:
        """Несколько последних материалов — US-2. Ничего не сохраняем."""
        adapters = build_adapters(self.config, limiter)
        adapter = adapters.get(resolution.kind)
        if adapter is None:
            return [], []
        probe_source = Source(
            id=0, name=resolution.name, url=resolution.fetch_url, kind=resolution.kind,
            fetch_url=resolution.fetch_url,
        )
        try:
            fetched = adapter.fetch(
                probe_source, FetchState(source_id=0), client, now=utc_now(), since=None
            )
        except Exception as e:  # источник может быть каким угодно — превью не должно ронять probe
            log.warning("превью недоступно для %s: %s", resolution.fetch_url, e)
            return [], ["preview_failed"]
        if fetched.error:
            code = (
                "telegram_preview_unavailable"
                if resolution.kind == "telegram"
                else "network_unreachable"
            )
            return [], [code]
        documents = fetched.documents[:PREVIEW_LIMIT]
        warnings = []
        if not documents:
            warnings.append("empty_feed")
        elif not any(d.published_at for d in documents):
            warnings.append("no_dates")
        return (
            [
                {"title": d.title, "url": d.url, "published_at": d.published_at}
                for d in documents
            ],
            warnings,
        )

    # -- жизненный цикл --

    def create(
        self,
        url: str,
        *,
        title: str = "",
        kind: str = "",
        fetch_url: str = "",
        category: str = "",
        poll_interval: str = "",
        category_hint: str | None = None,
        created_by: str = "",
    ) -> Source:
        """Создать источник. Дубль по `normalized_url` — 409, а не второй экземпляр."""
        if poll_interval and poll_interval not in POLL_INTERVALS:
            raise SourceValidationError(f"periodicity must be one of {list(POLL_INTERVALS)}"
            )
        if not kind or not fetch_url:
            probed = self.probe(url, with_preview=False)
            if probed.resolved_type == "unsupported":
                raise UnsupportedSourceError(f"не удалось определить, как опрашивать {url}",
                    {"note": probed.note},
                )
            kind = kind or probed.resolved_type
            fetch_url = fetch_url or probed.feed_url
            title = title or probed.title
            poll_interval = poll_interval or probed.suggested_poll_interval
        normalized = normalized_source_url(fetch_url or url)
        revived = self._revive(normalized, fetch_url or url)
        if revived is not None:
            return revived
        existing = self.db.sources.get_by_normalized(normalized)
        if existing is not None:
            raise SourceExistsError(f"{url} уже добавлен как источник #{existing.id} «{existing.name}»",
                {"source_id": existing.id},
            )
        now = to_utc_iso(utc_now()) or ""
        source = Source(
            name=title or url,
            url=url,
            kind=kind,
            category=category or _guess_category(url, kind),
            fetch_url=fetch_url or url,
            status="active",
            normalized_url=normalized,
            poll_interval=poll_interval or scheduler.DEFAULT_INTERVAL,
            next_run_at=now,
            category_hint=category_hint,
            created_at=now,
            created_by=created_by,
        )
        return self.db.sources.add(source)

    def _revive(self, normalized: str, fetch_url: str) -> Source | None:
        """Тот же адрес после мягкого удаления — это возврат источника, а не новый.

        Документы остались привязаны к старому `source_id`: заведи мы вторую
        строку, собранное осталось бы висеть на удалённом источнике.
        """
        for source in self.db.sources.list(status="deleted"):
            if source.normalized_url == normalized or source.fetch_url == fetch_url:
                log.info("источник #%s восстановлен вместо повторного добавления", source.id)
                return self.restore(source.id)
        return None

    def get(self, source_id: int) -> Source:
        source = self.db.sources.get(source_id)
        if source is None:
            raise SourceNotFoundError(f"источник #{source_id} не найден")
        return source

    def list(self, *, status: str | None = None, kind: str | None = None) -> list[Source]:
        if status and status not in SOURCE_STATUSES:
            raise SourceValidationError(f"status must be one of {list(SOURCE_STATUSES)}")
        return self.db.sources.list(status=status, kind=kind, include_deleted=status == "deleted")

    def update(
        self,
        source_id: int,
        *,
        name: str | None = None,
        poll_interval: str | None = None,
        category_hint: str | None = None,
        status: str | None = None,
    ) -> Source:
        """Переименовать, сменить частоту, поставить на паузу."""
        source = self.get(source_id)
        if poll_interval is not None:
            if poll_interval not in POLL_INTERVALS:
                raise SourceValidationError(f"periodicity must be one of {list(POLL_INTERVALS)}"
                )
            source.poll_interval = poll_interval
            # Смена интервала не ждёт конца текущего периода — иначе «поставил 15 минут»
            # начинает работать через сутки.
            source.next_run_at = scheduler.next_run_at(poll_interval)
        if name:
            source.name = name
        if category_hint is not None:
            source.category_hint = category_hint or None
        if status is not None:
            if status not in SOURCE_STATUSES:
                raise SourceValidationError(f"status must be one of {list(SOURCE_STATUSES)}"
                )
            source.status = status
            if status == "active" and not source.next_run_at:
                source.next_run_at = to_utc_iso(utc_now())
        self.db.sources.update(source)
        return source

    def pause(self, source_id: int) -> Source:
        return self.update(source_id, status="paused")

    def resume(self, source_id: int) -> Source:
        return self.update(source_id, status="active")

    def soft_delete(self, source_id: int, *, purge_items: bool = False) -> dict:
        """US-11: источник уходит, материалы остаются.

        `purge_items` прячет карточки из ленты, но тоже ничего не удаляет.
        """
        source = self.get(source_id)
        documents = self.db.documents.count(source_id)
        hidden = 0
        with self.db.transaction():
            self.db.sources.set_status(source_id, "deleted")
            if purge_items:
                hidden = self.db.items.hide_by_source(source_id)
        tracked = self.db.conn.execute(
            "SELECT count(*) FROM items i JOIN item_sources s ON s.item_id = i.id "
            "JOIN documents d ON d.id = s.document_id "
            "WHERE d.source_id = ? AND i.type = 'npa' AND i.is_archived = 0",
            (source_id,),
        ).fetchone()[0]
        if tracked:
            log.warning(
                "источник #%s питал %d отслеживаемых НПА — карточки останутся, но перестанут "
                "обновляться",
                source_id,
                tracked,
            )
        return {
            "source_id": source_id,
            "name": source.name,
            "documents_kept": documents,
            "items_hidden": hidden,
            "tracked_npa": tracked,
        }

    def restore(self, source_id: int) -> Source:
        source = self.get(source_id)
        source.status = "active"
        source.deleted_at = None
        source.next_run_at = to_utc_iso(utc_now())
        self.db.sources.update(source)
        return source

    # -- внеочередной опрос --

    def refresh(self, source_id: int, collector: Collector, *, force: bool = True) -> SourceRun:
        """Опросить источник прямо сейчас и вернуть запись прогона (контракт 1.4).

        Синхронно и на соединении запроса: для демо важно увидеть результат в
        ответе, а не в логе. Коллектор передаётся снаружи — сервис не знает, каким
        транспортом и с каким ключом его собрали.
        """
        source = self.get(source_id)
        if source.status == "deleted":
            raise SourceValidationError(f"источник #{source_id} удалён — сначала restore")
        collector.collect_one(source, force=force)
        runs = self.db.source_runs.history(source_id, 1)
        if not runs:
            raise SourceValidationError(f"опрос источника #{source_id} не оставил записи")
        return runs[0]

    # -- состояние --

    def health(self, source_id: int, limit: int = 20) -> dict:
        """История опросов: US-4 и ответ на риск незамеченного сбоя мониторинга."""
        source = self.get(source_id)
        state = self.db.fetch_state.get(source_id)
        runs = self.db.source_runs.history(source_id, limit)
        return {
            "source": source,
            "documents": self.db.documents.count(source_id),
            "consecutive_failures": state.consecutive_failures,
            "last_success_at": state.last_success_at,
            "last_error": state.last_error,
            "runs": runs,
        }


def _host_warnings(url: str) -> list[str]:
    low = (url or "").lower()
    warnings = []
    if any(host in low for host in PAYWALL_HOSTS):
        warnings.append("paywall_suspected")
    if any(host in low for host in BOT_HOSTS):
        warnings.append("bot_protection")
    return warnings


def _guess_category(url: str, kind: str) -> str:
    """Та же грубая эвристика, что в CLI: категория влияет только на дефолты."""
    low = (url or "").lower()
    if kind == "telegram":
        return "telegram"
    if kind == "manual":
        return "manual"
    if ".gov.ru" in low or "pravo.gov.ru" in low or "duma.gov.ru" in low or "cbr.ru" in low:
        return "regulator"
    return "media"


def run_backfill(config: Config, paths: ProjectPaths, source_id: int, *, tavily_key: str = "") -> None:
    """Первичный сбор после ответа `POST /sources`: добавление не должно висеть минуту.

    Фоновая задача открывает своё соединение — соединение запроса к этому моменту
    уже закрыто, а соединение SQLite принадлежит одному потоку.
    """
    db = Database(paths.db_path)
    try:
        source = db.sources.get(source_id)
        if source is not None:
            Collector(config, paths, db, tavily_key=tavily_key or None).collect_one(source)
    except Exception as e:  # фоновая задача не должна ронять процесс
        log.warning("первичный сбор источника #%s не удался: %s", source_id, e)
    finally:
        db.close()
