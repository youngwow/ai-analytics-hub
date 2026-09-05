"""Общие зависимости маршрутов: соединение на запрос и сервисы поверх него."""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Request

from ...processing.service import ProcessingService
from ...sources.manage import SourceService
from ...storage import Database


def get_db(request: Request) -> Iterator[Database]:
    """Соединение живёт ровно запрос — см. research.md, R-03."""
    context = request.app.state.context
    db = Database(context.paths.db_path)
    try:
        yield db
    finally:
        db.close()


def get_sources(request: Request, db: Database = Depends(get_db)) -> SourceService:
    return request.app.state.context.sources(db)


def get_processing(request: Request, db: Database = Depends(get_db)) -> ProcessingService:
    return request.app.state.context.processing(db)
