"""The model provider — the only module in the repository that imports `ollama`.

Everything above this file sees `LLMProvider` / `EmbeddingProvider` plus plain
dataclasses, so tests fake the model without the SDK on the import path
(constitution, principle I). Errors are split in two: a configuration failure
stops the run (a wrong key must not look like a network blip), a temporary one
is retried and then degrades to the extractive baseline.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

import httpx

from ..common import get_logger
from ..config import EmbeddingConfig, LLMConfig

log = get_logger("llm")

KEY_HINT = "положите ключ Ollama Cloud в .env как OLLAMA_API_KEY"
NO_KEY = f"нет ключа доступа к модели: {KEY_HINT}"
_CONFIG_STATUSES = (400, 401, 402, 403, 404, 422)


class LlmError(Exception):
    """Base class: something went wrong while talking to the model."""


class LlmConfigError(LlmError):
    """Wrong key, unknown model, exhausted quota — retrying cannot help."""


class LlmTemporaryError(LlmError):
    """Rate limit, server error, network hiccup — worth a retry, then degrade."""


@dataclass(frozen=True)
class Completion:
    """One structured-output answer plus the telemetry behind the 15 s budget."""

    data: dict
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0


class LLMProvider(Protocol):
    def complete(self, prompt: str, schema: dict, *, system: str = "") -> Completion: ...


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass
class OllamaProvider:
    """GLM-5.3 in Ollama Cloud (research.md, R-01/R-02/R-03).

    The schema is passed as `format`, but do not count on it: Ollama Cloud accepts
    the parameter and ignores it, so a plain-text or markdown-fenced answer can and
    does come back. `extract_json` below digs the object out and `schema.py` decides
    whether it means anything. Against a local server the same `format` really does
    constrain generation.
    """

    config: LLMConfig
    api_key: str = ""
    sleep: Any = time.sleep
    _client: Any = field(default=None, init=False, repr=False)

    def _connect(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise LlmConfigError(NO_KEY)
        from ollama import Client  # imported here so nothing else depends on the SDK

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        self._client = Client(
            host=self.config.host, headers=headers, timeout=self.config.request_timeout
        )
        return self._client

    def _call(self, what: str, fn):
        """Run one request, translating SDK failures and retrying the temporary ones."""
        import ollama

        delay = self.config.retry_backoff
        last: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            started = time.monotonic()
            try:
                return fn(), int((time.monotonic() - started) * 1000)
            except ollama.ResponseError as e:
                status = getattr(e, "status_code", 0) or 0
                if status in _CONFIG_STATUSES:
                    raise LlmConfigError(f"{what}: HTTP {status} — {e}") from e
                last = LlmTemporaryError(f"{what}: HTTP {status} — {e}")
            except ollama.RequestError as e:
                raise LlmConfigError(f"{what}: некорректный запрос — {e}") from e
            except httpx.HTTPError as e:
                # httpx timeouts inherit from neither TimeoutError nor OSError,
                # so they need naming explicitly or they escape as a crash.
                last = LlmTemporaryError(f"{what}: {type(e).__name__} — {e}")
            except (ConnectionError, TimeoutError, OSError) as e:
                last = LlmTemporaryError(f"{what}: сеть недоступна — {e}")
            if attempt < self.config.max_retries:
                log.warning("%s: %s; повтор через %.1f с", what, last, delay)
                self.sleep(delay)
                delay *= 2
        raise last or LlmTemporaryError(what)

    def complete(self, prompt: str, schema: dict, *, system: str = "") -> Completion:
        client = self._connect()
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        options: dict = {"temperature": self.config.temperature}
        if self.config.max_output_tokens > 0:
            options["num_predict"] = self.config.max_output_tokens
        extra = {} if self.config.think is None else {"think": self.config.think}
        started = time.monotonic()
        invalid: LlmTemporaryError | None = None
        for attempt in range(self.config.max_retries + 1):
            response, _ = self._call(
                "chat",
                lambda: client.chat(
                    model=self.config.model,
                    messages=messages,
                    format=schema,
                    options=options,
                    **extra,
                ),
            )
            raw = _message_content(response)
            data = extract_json(raw)
            if isinstance(data, dict):
                return Completion(
                    data=data,
                    model=self.config.model,
                    tokens_in=_int_field(response, "prompt_eval_count"),
                    tokens_out=_int_field(response, "eval_count"),
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            invalid = LlmTemporaryError(
                "модель вернула не объект JSON"
                if data is not None
                else f"модель вернула не JSON: {raw[:200]}"
            )
            if attempt < self.config.max_retries:
                log.warning("chat: %s; повтор через %.1f с", invalid, self.config.retry_backoff)
                self.sleep(self.config.retry_backoff)
        raise invalid or LlmTemporaryError("модель не вернула JSON")

    def close(self) -> None:
        self._client = None


@dataclass
class OpenRouterEmbeddingProvider:
    """Gemini embeddings through OpenRouter; never performs generation."""

    config: EmbeddingConfig
    api_key: str = ""
    sleep: Any = time.sleep
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    def _connect(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise LlmConfigError(
                "нет ключа embeddings: положите OPENROUTER_API_KEY в .env"
            )
        self._client = httpx.Client(
            base_url=self.config.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.config.request_timeout,
        )
        return self._client

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._connect()
        delay = self.config.retry_backoff
        last: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = client.post(
                    "embeddings",
                    json={"model": self.config.model, "input": list(texts)},
                )
                if response.status_code in _CONFIG_STATUSES:
                    raise LlmConfigError(
                        f"embeddings: HTTP {response.status_code} — {response.text[:200]}"
                    )
                response.raise_for_status()
                payload = response.json()
                rows = sorted(payload.get("data", []), key=lambda row: row.get("index", 0))
                vectors = [[float(x) for x in row.get("embedding", [])] for row in rows]
                if len(vectors) != len(texts) or any(
                    len(vector) != self.config.dimensions for vector in vectors
                ):
                    raise LlmTemporaryError(
                        "embeddings: неверное число векторов или размерность"
                    )
                return vectors
            except LlmConfigError:
                raise
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                last = LlmTemporaryError(f"embeddings: {type(exc).__name__} — {exc}")
            if attempt < self.config.max_retries:
                self.sleep(delay)
                delay *= 2
        raise last or LlmTemporaryError("embeddings")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _field(response: Any, name: str) -> Any:
    """Ollama returns pydantic models; dicts arrive from fakes and from replays."""
    if isinstance(response, dict):
        return response.get(name)
    return getattr(response, name, None)


def _int_field(response: Any, name: str) -> int:
    try:
        return int(_field(response, name) or 0)
    except (TypeError, ValueError):
        return 0


def extract_json(content: str) -> dict | None:
    """Pull the answer object out of whatever the model wrapped it in.

    Ollama Cloud honours `format` for no model at all, so answers arrive wrapped
    in prose or in a markdown fence; the object is located by scanning braces
    rather than by trusting the response to be bare JSON.
    """
    text = (content or "").strip()
    if not text:
        return None
    for candidate in _json_candidates(text):
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _json_candidates(text: str):
    """Whole string first, then every balanced {...} block, longest first."""
    yield text
    blocks: list[str] = []
    depth = start = 0
    in_string = escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                blocks.append(text[start : index + 1])
            elif depth < 0:
                depth = 0
    yield from sorted(blocks, key=len, reverse=True)


def _message_content(response: Any) -> str:
    message = _field(response, "message")
    if message is None:
        return ""
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or "")


def build_llm_provider(config: LLMConfig, api_key: str) -> OllamaProvider:
    return OllamaProvider(config=config, api_key=api_key)


def build_embedding_provider(
    config: EmbeddingConfig, api_key: str
) -> OpenRouterEmbeddingProvider:
    return OpenRouterEmbeddingProvider(config=config, api_key=api_key)


# Compatibility for callers that only need generation.
build_provider = build_llm_provider
