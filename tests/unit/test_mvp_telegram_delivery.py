from __future__ import annotations

import json

import pytest

from MVP.backend.delivery import (
    TELEGRAM_MESSAGE_LIMIT,
    TelegramBotDeliveryAdapter,
    render_telegram_digest,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def _payload(text: str = "Важный сигнал") -> dict:
    return {
        "id": "weekly-demo",
        "version": 1,
        "title": "Повестка GS Labs",
        "period": {"from": "2026-09-01", "to": "2026-09-07"},
        "header": "Коротко о главном",
        "role_content": {
            "PR": {"text": f"{text}\n[Источник](https://example.test/pr)"},
            "GR": {"text": "Изменение НПА\n[Источник](https://example.test/gr)"},
        },
        "footer": "Подготовлено системой",
    }


def test_telegram_adapter_sends_to_default_channel() -> None:
    captured = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response({"ok": True, "result": {"message_id": 42}})

    result = TelegramBotDeliveryAdapter(
        "fake-token", "@gs_demo", opener=opener
    ).send("default", _payload())

    assert captured["url"].endswith("/botfake-token/sendMessage")
    assert captured["body"]["chat_id"] == "@gs_demo"
    assert "Источник — https://example.test/pr" in captured["body"]["text"]
    assert result == "telegram://@gs_demo/42"


def test_telegram_message_is_safely_clipped() -> None:
    message = render_telegram_digest(_payload("Сигнал " * 2000))
    assert len(message) <= TELEGRAM_MESSAGE_LIMIT
    assert message.endswith("Полная версия доступна в интерфейсе.")


@pytest.mark.parametrize(
    ("token", "chat_id", "error"),
    [
        ("", "@channel", "TELEGRAM_BOT_TOKEN"),
        ("token", "", "TELEGRAM_CHAT_ID"),
    ],
)
def test_telegram_adapter_requires_credentials(token: str, chat_id: str, error: str) -> None:
    with pytest.raises(RuntimeError, match=error):
        TelegramBotDeliveryAdapter(token, chat_id).send("default", _payload())
