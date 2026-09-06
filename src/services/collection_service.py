"""Автоматический мониторинг из процесса API: `collect --watch` как фоновый поток.

Отдельного планировщика по-прежнему нет (research.md, R-05): поток тикает и
спрашивает базу, чья очередь (`scheduler.due`). Состояние «запущен/остановлен»
живёт в процессе — оно и есть свойство процесса; каждый цикл записывается в
`collect_runs`, поэтому история переживает перезапуск. Циклы не пересекаются:
общий замок на процесс, кто пришёл вторым — получает 409.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta
from typing import Callable

from ..config import Config
from ..exceptions import (
    CollectionBusyError,
    CollectionNotRunningError,
    CollectionRunningError,
    CollectionValidationError,
)
from ..models import CollectReport
from ..paths import ProjectPaths
from ..repositories import Database
from ..sources import scheduler
from ..sources.collector import Collector
from ..utils import get_logger, to_utc_iso, utc_now

log = get_logger("collection")

MIN_INTERVAL_SECONDS = 60
DEFAULT_INTERVAL_SECONDS = 900

# Один цикл сбора на процесс: тик наблюдателя и разовый запрос не должны опрашивать
# одни и те же источники одновременно.
_CYCLE_LOCK = threading.Lock()


def run_collection_cycle(
    config: Config,
    paths: ProjectPaths,
    *,
    tavily_key: str = "",
    due_only: bool = True,
    source_ids: list[int] | None = None,
    backfill: bool = False,
    force: bool = False,
    date_window_hours: int | None = None,
) -> CollectReport:
    """Один проход коллектора на своём соединении (поток — не тот, что у запроса)."""
    if date_window_hours is not None:
        if date_window_hours < 1:
            raise CollectionValidationError("date_window_hours должен быть не меньше 1")
        config = replace(config, scraper=replace(config.scraper, date_window_hours=date_window_hours))
    if not _CYCLE_LOCK.acquire(blocking=False):
        raise CollectionBusyError()
    try:
        db = Database(paths.db_path)
        try:
            collector = Collector(config, paths, db, tavily_key=tavily_key or None)
            return collector.run(
                source_ids=source_ids, backfill=backfill, force=force, due_only=due_only
            )
        finally:
            db.close()
    finally:
        _CYCLE_LOCK.release()


def run_collection_task(config: Config, paths: ProjectPaths, **kwargs) -> None:
    """Цель `BackgroundTasks` для `POST /collection/runs`: ошибка — в лог, не в процесс."""
    try:
        report = run_collection_cycle(config, paths, **kwargs)
        log.info("разовый цикл сбора: %s", report.summary_line())
    except Exception as e:  # фоновая задача не должна ронять процесс
        log.warning("разовый цикл сбора не удался: %s", e)


def cycle_in_progress() -> bool:
    return _CYCLE_LOCK.locked()


class CollectionWatcher:
    """Фоновый цикл: раз в `interval_seconds` опросить тех, чья очередь пришла."""

    def __init__(self, run_cycle: Callable[[], CollectReport]):
        self._run_cycle = run_cycle
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self.date_window_hours: int | None = None
        self.interval_seconds = DEFAULT_INTERVAL_SECONDS
        self.started_at: str | None = None
        self.next_tick_at: str | None = None
        self.cycles = 0
        self.last_error = ""
        self.busy = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, interval_seconds: int = DEFAULT_INTERVAL_SECONDS, *, date_window_hours: int | None = None) -> None:
        if interval_seconds < MIN_INTERVAL_SECONDS:
            raise CollectionValidationError(
                f"interval_seconds должен быть не меньше {MIN_INTERVAL_SECONDS}"
            )
        with self._guard:
            if self.running:
                raise CollectionRunningError()
            self.date_window_hours = date_window_hours
            self.interval_seconds = int(interval_seconds)
            self._stop.clear()
            self.started_at = to_utc_iso(utc_now())
            self.next_tick_at = self.started_at
            self.last_error = ""
            self._thread = threading.Thread(
                target=self._loop, name="collection-watcher", daemon=True
            )
            self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        with self._guard:
            if not self.running:
                raise CollectionNotRunningError()
            self._stop.set()
            thread = self._thread
        if thread is not None and threading.current_thread() is not thread:
            thread.join(timeout)
        self._thread = None
        self.next_tick_at = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self.next_tick_at = to_utc_iso(utc_now() + timedelta(seconds=self.interval_seconds))
            self._stop.wait(self.interval_seconds)

    def tick(self) -> CollectReport | None:
        """Один цикл; ошибка не останавливает наблюдатель, а остаётся в `last_error`."""
        self.busy = True
        try:
            report = self._run_cycle()
            self.cycles += 1
            self.last_error = ""
            return report
        except CollectionBusyError:
            return None  # разовый запрос уже опрашивает — этот тик пропускаем
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"[:500]
            log.warning("цикл сбора не удался: %s", self.last_error)
            return None
        finally:
            self.busy = False

    def status(self) -> dict:
        return {
            "running": self.running,
            "busy": self.busy or cycle_in_progress(),
            "interval_seconds": self.interval_seconds,
            "started_at": self.started_at if self.running else None,
            "next_tick_at": self.next_tick_at if self.running else None,
            "cycles": self.cycles,
            "last_error": self.last_error,
        }


class CollectionService:
    """Что видит кнопка «Запустить автоматический мониторинг»."""

    def __init__(self, config: Config, db: Database, watcher: CollectionWatcher):
        self.config = config
        self.db = db
        self.watcher = watcher

    def status(self) -> dict:
        latest = self.db.runs.latest()
        return {
            **self.watcher.status(),
            "date_window_hours": self.watcher.date_window_hours or self.config.scraper.date_window_hours,
            "due_sources": len(scheduler.due(self.db)),
            "last_collect": dict(latest) if latest else None,
        }

    def start(self, interval_seconds: int = DEFAULT_INTERVAL_SECONDS, date_window_hours: int | None = None) -> dict:
        if date_window_hours is not None and date_window_hours < 1:
            raise CollectionValidationError("date_window_hours должен быть не меньше 1")
        self.watcher.start(interval_seconds, date_window_hours=date_window_hours or self.config.scraper.date_window_hours)
        log.info("автоматический мониторинг запущен: каждые %d с", interval_seconds)
        return self.status()

    def stop(self) -> dict:
        self.watcher.stop()
        log.info("автоматический мониторинг остановлен")
        return self.status()

    def ensure_idle(self) -> None:
        """Перед разовым циклом: второй цикл рядом с идущим — это 409, а не гонка."""
        if self.watcher.busy or cycle_in_progress():
            raise CollectionBusyError()
