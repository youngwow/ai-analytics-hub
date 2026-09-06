from __future__ import annotations

from dataclasses import replace

from src.config import LLMConfig
from src.processing.llm import OllamaProvider


class FakeClient:
    def __init__(self):
        self.calls = 0

    def chat(self, **_kwargs):
        self.calls += 1
        content = "" if self.calls == 1 else '{"status":"ok"}'
        return {
            "message": {"content": content},
            "prompt_eval_count": 3,
            "eval_count": 2,
        }


def test_structurally_invalid_completion_is_retried():
    config = replace(LLMConfig(), max_retries=1, retry_backoff=0)
    provider = OllamaProvider(config=config, api_key="unused", sleep=lambda _: None)
    client = FakeClient()
    provider._client = client

    completion = provider.complete("prompt", {"type": "object"})

    assert client.calls == 2
    assert completion.data == {"status": "ok"}
