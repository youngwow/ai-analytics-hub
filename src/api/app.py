"""Сборка приложения и единый формат ошибок.

Ошибки отдаются как `application/problem+json` (RFC 7807) — этого требует принцип
III конституции. Машинный `code` из спецификации сохраняется расширением, чтобы
UI ветвился по коду, а не по тексту (research.md, R-02).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..common import get_logger
from ..config import Config
from ..paths import ProjectPaths
from ..processing.service import ItemError
from ..sources.manage import SourceError
from .deps import Context
from .routers import items as items_router
from .routers import sources as sources_router

log = get_logger("api")

PROBLEM_JSON = "application/problem+json"
# Код ошибки → HTTP-статус. Всё, чего здесь нет, это 400.
STATUS_BY_CODE = {
    "source_exists": 409,
    "possible_duplicate": 409,
    "nothing_to_revert": 409,
    "source_not_found": 404,
    "item_not_found": 404,
    "unsupported_source": 422,
    "network_unreachable": 502,
    "telegram_preview_unavailable": 502,
    "validation_error": 400,
}
TITLES = {
    400: "Некорректный запрос",
    404: "Не найдено",
    409: "Конфликт",
    422: "Не удалось обработать",
    500: "Внутренняя ошибка",
    502: "Внешний источник недоступен",
}


def problem(status: int, detail: str, code: str = "", details: dict | None = None) -> JSONResponse:
    body = {
        "type": "about:blank",
        "title": TITLES.get(status, "Ошибка"),
        "status": status,
        "detail": detail,
    }
    if code:
        body["code"] = code
    if details:
        body["details"] = details
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_JSON)


def create_app(config: Config | None = None, paths: ProjectPaths | None = None) -> FastAPI:
    context = Context(config, paths)
    app = FastAPI(
        title="ai-analytics-hub",
        version="1.4",
        description="Управление источниками и данными (этап 1.4)",
        docs_url="/docs" if context.config.api.docs else None,
        openapi_url="/openapi.json" if context.config.api.docs else None,
    )
    app.state.context = context

    @app.exception_handler(SourceError)
    @app.exception_handler(ItemError)
    async def service_error(request: Request, exc) -> JSONResponse:
        return problem(
            STATUS_BY_CODE.get(exc.code, 400), exc.message, exc.code, exc.details
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(422, "тело запроса не прошло проверку", "validation_error",
                       {"errors": exc.errors()[:5]})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(exc.status_code, str(exc.detail))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("необработанная ошибка на %s", request.url.path)
        return problem(500, "внутренняя ошибка сервиса")

    @app.get("/api/v1/health", tags=["service"])
    def health() -> dict:
        return {"status": "ok", "version": app.version}

    app.include_router(sources_router.router)
    app.include_router(items_router.router)
    return app


app = create_app()
