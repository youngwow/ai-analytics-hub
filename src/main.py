"""Сборка приложения: фабрика, lifespan, CORS и единый обработчик ошибок.

Экземпляра на уровне модуля нет нарочно: uvicorn получает фабрику
(`uvicorn src.main:create_app --factory`), поэтому импорт модуля при сборе
тестов не собирает приложение против реального корня и не читает `.env`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import api_router
from .api.problem import problem
from .config import get_config, get_paths, get_settings
from .dependencies import get_llm_provider
from .exceptions import AppError
from .repositories import Database
from .utils import configure_logging, get_logger

log = get_logger("api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Прогреть зависимости до первого запроса, отпустить при остановке."""
    settings, paths = get_settings(), get_paths()
    get_config()  # ConfigError всплывает здесь, а не на первом запросе
    db = Database(paths.db_path)  # открывает и мигрирует схему на этом потоке
    version = db.conn.execute("PRAGMA user_version").fetchone()[0]
    db.close()  # соединения живут по запросу; это никогда не хранится в app.state
    log.info(
        "запуск %s (%s): корень %s, схема v%s", settings.app_name, settings.environment,
        paths.root, version,
    )
    yield
    provider = get_llm_provider()
    if provider is not None:
        provider.close()
    log.info("остановка завершена")


def create_app() -> FastAPI:
    """Фабрика приложения — тесты собирают свой экземпляр, а не импортируют глобальный."""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="Интеллектуальный аналитический центр: источники, карточки, лента",
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs else None,
        openapi_url="/openapi.json" if settings.docs else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        """Единственная точка перевода ошибок домена в HTTP."""
        return problem(exc.status_code, exc.detail, exc.code, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(
            422, "тело запроса не прошло проверку", "validation_error",
            {"errors": exc.errors()[:5]},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(exc.status_code, str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("необработанная ошибка на %s", request.url.path)
        return problem(500, "внутренняя ошибка сервиса")

    app.include_router(api_router, prefix=settings.api_prefix)
    return app


def main() -> None:
    """Сервер разработки: ``uv run python -m src.main`` (или ``python -m src serve``)."""
    import uvicorn

    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        "src.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=settings.environment == "local",
    )


if __name__ == "__main__":
    main()
