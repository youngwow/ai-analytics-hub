"""HTTP-слой: маршруты — тонкие обёртки над методами сервисов.

Обработчики только разбирают запрос и зовут метод сервиса; бизнес-логика живёт в
`src/services/` (принцип III конституции). Префикс `/api/v1` навешивается один
раз в `create_app()`, роутеры несут только свой ресурсный префикс.
"""

from fastapi import APIRouter

from .routes import collection, feed, health, items, processing, sources

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(sources.router)
api_router.include_router(items.router)
api_router.include_router(feed.router)
api_router.include_router(processing.router)
api_router.include_router(collection.router)

__all__ = ["api_router"]
