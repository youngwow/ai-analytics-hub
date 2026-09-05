"""Единый формат ошибок: `application/problem+json` (RFC 7807, принцип III).

Машинный `code` и `details` — расширения, чтобы UI ветвился по коду, а не по
тексту (research.md, R-02). Статус живёт на классе ошибки (`src/exceptions.py`).
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

PROBLEM_JSON = "application/problem+json"
TITLES = {
    400: "Некорректный запрос",
    404: "Не найдено",
    409: "Конфликт",
    422: "Не удалось обработать",
    500: "Внутренняя ошибка",
    502: "Внешний источник недоступен",
    503: "Сервис не готов",
}


def problem(status: int, detail: str, code: str = "", details: dict | None = None) -> JSONResponse:
    body: dict = {
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
