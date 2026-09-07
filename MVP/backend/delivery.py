"""External delivery adapters for the pilot MVP."""

from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TELEGRAM_MESSAGE_LIMIT = 4096
_SOURCE_LINK = re.compile(r"^\[([^]]+)]\((https?://[^)]+)\)$")


def _plain_text(value: object) -> str:
    """Keep generated source links clickable without relying on Telegram Markdown."""
    lines: list[str] = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        match = _SOURCE_LINK.fullmatch(line)
        lines.append(f"{match.group(1)} — {match.group(2)}" if match else raw_line)
    return "\n".join(lines).strip()


def render_telegram_digest(payload: dict[str, Any]) -> str:
    period = payload.get("period") or {}
    period_text = " — ".join(
        value for value in (str(period.get("from") or ""), str(period.get("to") or "")) if value
    )
    role_content = payload.get("role_content") or {}
    parts = [str(payload.get("title") or "Дайджест GS Labs")]
    if period_text:
        parts.append(period_text)
    if payload.get("header"):
        parts.append(_plain_text(payload["header"]))
    for role in ("PR", "GR"):
        text = _plain_text((role_content.get(role) or {}).get("text"))
        if text:
            parts.append(f"{role}\n{text}")
    if payload.get("footer"):
        parts.append(_plain_text(payload["footer"]))
    message = "\n\n".join(part for part in parts if part.strip()).strip()
    if len(message) > TELEGRAM_MESSAGE_LIMIT:
        suffix = "\n\nПолная версия доступна в интерфейсе."
        message = message[: TELEGRAM_MESSAGE_LIMIT - len(suffix)].rstrip() + suffix
    return message


class TelegramBotDeliveryAdapter:
    """Send one approved digest to a Telegram chat or channel via Bot API."""

    def __init__(
        self,
        bot_token: str,
        default_chat_id: str,
        *,
        opener: Callable[..., Any] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.bot_token = bot_token.strip()
        self.default_chat_id = default_chat_id.strip()
        self.opener = opener or urlopen
        self.timeout = timeout

    def send(self, recipient: str, payload: dict[str, Any]) -> str:
        if not self.bot_token:
            raise RuntimeError("не задан TELEGRAM_BOT_TOKEN")
        chat_id = recipient.strip()
        if chat_id in {"", "default"}:
            chat_id = self.default_chat_id
        if not chat_id:
            raise RuntimeError("не задан TELEGRAM_CHAT_ID")
        body = json.dumps(
            {
                "chat_id": chat_id,
                "text": render_telegram_digest(payload),
                "disable_web_page_preview": True,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"Telegram вернул HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError("Telegram недоступен") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Telegram вернул некорректный ответ") from exc
        if not result.get("ok"):
            description = str(result.get("description") or "неизвестная ошибка")[:200]
            raise RuntimeError(f"Telegram отклонил сообщение: {description}")
        message_id = (result.get("result") or {}).get("message_id", "unknown")
        return f"telegram://{chat_id}/{message_id}"
