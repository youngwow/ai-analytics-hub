"""Shared, deliberately boring helpers for the ingestion and processing tests.

`MockRoutes` is a URL → canned-reply table behind `httpx.MockTransport` that also
records every request, so tests can assert on headers, bodies and call order
without touching the network. `FakeLLM` is the same idea for stage 1.2: it
implements `LLMProvider` / `EmbeddingProvider` with canned answers and a call
log, so no test ever imports `ollama` or spends a token. The small builders at
the bottom produce inline feeds / pages for cases the on-disk fixtures do not
cover.
"""

from __future__ import annotations

import copy
import os
import sqlite3
from typing import Callable, Sequence

import httpx

from src.processing.llm import Completion
from src.repositories.database import _MIGRATIONS

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

Reply = tuple[int, bytes, dict[str, str]]
Handler = Callable[[httpx.Request], Reply]

HTML_UTF8 = {"content-type": "text/html; charset=utf-8"}
HTML_CP1251 = {"content-type": "text/html; charset=windows-1251"}
XML = {"content-type": "application/xml"}
RSS = {"content-type": "application/rss+xml"}
GZIP = {"content-type": "application/x-gzip"}
JSON = {"content-type": "application/json"}

# A `t.me/s/<channel>` page with the channel header but no posts — what Telegram
# serves for `?after=<newest id>` when nothing newer exists.
EMPTY_CHANNEL_PAGE = (
    '<!DOCTYPE html><html><body class="tgme_page_body">'
    '<div class="tgme_channel_info"><div class="tgme_channel_info_header">'
    '<div class="tgme_channel_info_header_title"><span dir="auto">'
    "Цифровые индустриальные технологии"
    "</span></div></div></div></body></html>"
).encode("utf-8")


def read_fixture(name: str) -> bytes:
    with open(os.path.join(FIXTURES_DIR, name), "rb") as f:
        return f.read()


def frozen_database(path: str, version: int) -> sqlite3.Connection:
    """Открыть базу, застывшую на `PRAGMA user_version = version`.

    Это установка, которую ещё не трогал новый шаг миграции: схема собрана
    скриптами до `version` включительно, дальше — ничего. Тест наполняет её
    сам и открывает уже через `Database`, чтобы увидеть настоящий переезд.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for step in range(1, version + 1):
        conn.executescript(_MIGRATIONS[step])
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    return conn


def default_raw_config() -> dict:
    """A fresh dict mirroring config.yaml (every key `Config.from_dict` requires)."""
    return {
        "scraper": {
            "date_window_hours": 72,
            "request_timeout": 20,
            "max_redirects": 5,
            "user_agent": "test-agent",
            "accept_language": "ru",
            "concurrency": 4,
            "per_host_concurrency": 2,
            "fetch_fulltext": True,
            "fulltext_timeout": 5,
            "fulltext_max_chars": 20000,
            "max_new_per_source": 50,
            "store_raw_html": False,
        },
        "telegram": {"backfill_pages": 1},
        "sitemap": {"max_sitemaps": 5, "max_urls": 200},
        "tavily": {"api_key_env": "TAVILY_API", "max_results": 10, "search_depth": "basic"},
        # Every key of `llm` / `processing` has a default; tests that care set
        # the one key they are about.
        "llm": {},
        "processing": {"concurrency": 1},
    }


def raising(exc: Exception) -> Handler:
    """A route value that makes the transport raise `exc` (timeouts, connection errors)."""

    def handler(request: httpx.Request) -> Reply:
        raise exc

    return handler


class MockRoutes:
    """URL table for `httpx.MockTransport`.

    Keys are matched in this order: the full URL, `path?query`, bare `path`, then
    any key ending in `*` as a prefix of the full URL. Values are
    `(status, body, headers)` or a callable taking the request and returning that
    tuple. Unmatched URLs get `default` (404). Every request is appended to
    `requests` in call order.
    """

    def __init__(
        self,
        routes: dict[str, Reply | Handler] | None = None,
        default: Reply = (404, b"not found", {}),
    ):
        self.routes: dict[str, Reply | Handler] = dict(routes or {})
        self.default = default
        self.requests: list[httpx.Request] = []

    def __setitem__(self, key: str, value: Reply | Handler) -> None:
        self.routes[key] = value

    def _lookup(self, request: httpx.Request) -> Reply | Handler:
        url = request.url
        full = str(url)
        query = url.query.decode()
        for key in (full, url.path + (f"?{query}" if query else ""), url.path):
            if key in self.routes:
                return self.routes[key]
        for key, value in self.routes.items():
            if key.endswith("*") and full.startswith(key[:-1]):
                return value
        return self.default

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self._lookup(request)
        if callable(reply):
            reply = reply(request)
        status, body, headers = reply
        return httpx.Response(status, content=body, headers=headers, request=request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self, **kwargs) -> httpx.Client:
        return httpx.Client(transport=self.transport(), **kwargs)

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    def requests_to(self, url: str) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url) == url]


def rss_bytes(items: list[dict], title: str = "Тестовая лента") -> bytes:
    """Build a minimal RSS 2.0 feed. Item keys: title, link, guid, pubDate, description, text."""
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss version="2.0" xmlns:yandex="http://news.yandex.ru"><channel>',
        f"<title>{title}</title><link>https://example.ru/</link>",
    ]
    for item in items:
        parts.append("<item>")
        parts.append(f"<title>{item.get('title', 'Заголовок')}</title>")
        if "link" in item:
            parts.append(f"<link>{item['link']}</link>")
        if "guid" in item:
            parts.append(f'<guid isPermaLink="false">{item["guid"]}</guid>')
        if "pubDate" in item:
            parts.append(f"<pubDate>{item['pubDate']}</pubDate>")
        if "description" in item:
            parts.append(f"<description>{item['description']}</description>")
        if "text" in item:
            parts.append(f"<yandex:full-text>{item['text']}</yandex:full-text>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "\n".join(parts).encode("utf-8")


def html_page(title: str, body: str, *, published: str | None = None) -> bytes:
    """A small article page; `published` fills `article:published_time` when given."""
    meta = f'<meta property="article:published_time" content="{published}">' if published else ""
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{title}</title>{meta}</head>"
        f"<body><main><article><h1>{title}</h1>{body}</article></main></body></html>"
    ).encode("utf-8")


# ── stage 1.2: the model, faked ────────────────────────────────────────────

FAKE_MODEL = "fake-glm:test"

# Already normalised: single spaces, no boilerplate, no markup — so
# `normalize.normalize(ARTICLE_TEXT) == ARTICLE_TEXT` and the offsets below hold
# both for `Pipeline.process(norm_text)` and for a document stored verbatim.
ARTICLE_LINES = (
    "Минцифры внесло в правительство законопроект № 112233-8 о регулировании оборота ИИ-сервисов.",
    "Документ обязывает операторов ИИ проходить обязательную аккредитацию с 1 января 2027 года.",
    "Требование распространяется на компании из реестра отечественного ПО, включая разработчиков систем защиты контента.",
    "Ко второму чтению профильный комитет обещает уточнить перечень исключений.",
)
ARTICLE_TEXT = "\n".join(ARTICLE_LINES)

# The same act, a later publication from another outlet: different wording, so
# SimHash keeps it apart and only `npa_key` joins the two cards.
LATER_NPA_LINES = (
    "Совет Думы включил в календарь рассмотрение инициативы об аккредитации ИИ-сервисов.",
    "Профильный комитет анонсировал сбор отзывов у отраслевых ассоциаций до конца октября.",
    "Ответственным назначен комитет по информационной политике, срок отзывов открыт.",
)
LATER_NPA_TEXT = "\n".join(LATER_NPA_LINES)

NEWS_LINES = (
    "Оператор платного ТВ запустил бета-версию рекомендательного сервиса на собственных моделях.",
    "Компания обещает подключить к нему абонентов спутникового вещания в течение квартала.",
    "Рекомендации строятся на обезличенной истории просмотров и не выходят за пределы приставки.",
)
NEWS_TEXT = "\n".join(NEWS_LINES)


def spans(text: str, *fragments: str) -> list[list[int]]:
    """`evidence_offsets` for `fragments`, in order, as the model would report them."""
    out = []
    for fragment in fragments:
        start = text.index(fragment)
        out.append([start, start + len(fragment)])
    return out


def npa_answer(**overrides) -> dict:
    """A well-formed S2-S5 answer about `ARTICLE_TEXT`; every field is overridable."""
    base = {
        "type": "npa",
        "npa_status": "внесён",
        "npa_key": "112233-8",
        "summary": [
            "Минцифры внесло законопроект № 112233-8 о регулировании ИИ-сервисов.",
            "Операторы ИИ обязаны пройти аккредитацию с 1 января 2027 года.",
            "Требование затрагивает компании из реестра отечественного ПО.",
        ],
        "entities": {
            "who": "Минцифры",
            "what": "законопроект № 112233-8 о регулировании ИИ-сервисов",
            "when": "1 января 2027 года",
            "impact": "обязательная аккредитация операторов ИИ",
        },
        "priority": "high",
        "relevance_score": 0.86,
        "reasoning": "Затрагивает реестр отечественного ПО и аккредитацию ИТ-компаний.",
        "matched_profile_facets": ["реестр отечественного ПО"],
        "tags": ["регуляторика"],
        "confidence": 0.81,
        "evidence_offsets": spans(ARTICLE_TEXT, *ARTICLE_LINES[:3]),
    }
    return {**base, **overrides}


def news_answer(**overrides) -> dict:
    """A well-formed answer about `NEWS_TEXT` (type=news, no act identifier)."""
    base = {
        "type": "news",
        "summary": [
            "Оператор платного ТВ запустил бета-версию рекомендательного сервиса.",
            "Компания обещает подключить абонентов спутникового вещания в течение квартала.",
            "Рекомендации строятся на обезличенной истории просмотров.",
        ],
        "entities": {
            "who": "Оператор платного ТВ",
            "what": "бета-версия рекомендательного сервиса",
            "when": "в течение квартала",
            "impact": "рекомендации на приставке",
        },
        "priority": "medium",
        "relevance_score": 0.62,
        "reasoning": "Конкурентная активность на рынке платного ТВ.",
        "matched_profile_facets": [],
        "tags": ["конкуренты"],
        "confidence": 0.7,
        "evidence_offsets": spans(NEWS_TEXT, *NEWS_LINES),
    }
    return {**base, **overrides}


def later_npa_answer(**overrides) -> dict:
    """A second publication about the same act — same `npa_key`, earlier status."""
    base = npa_answer(
        npa_status="анонс",
        summary=[
            "Совет Думы включил рассмотрение инициативы об аккредитации ИИ-сервисов в календарь.",
            "Профильный комитет анонсировал сбор отзывов у отраслевых ассоциаций.",
            "Ответственным назначен комитет по информационной политике.",
        ],
        evidence_offsets=spans(LATER_NPA_TEXT, *LATER_NPA_LINES),
        relevance_score=0.7,
        confidence=0.66,
    )
    return {**base, **overrides}


class FakeLLM:
    """Offline stand-in for `LLMProvider` + `EmbeddingProvider`.

    `answers` are handed out by `complete()` in order and the last one repeats,
    so a run over many documents needs a single canned reply. An `Exception` in
    the list is raised instead of returned, and a callable is given the prompt
    and returns the answer dict. Every prompt, system message and schema is
    recorded, which is what the privacy tests assert on.
    """

    def __init__(
        self,
        answers=None,
        *,
        model: str = FAKE_MODEL,
        embedder=None,
        tokens: tuple[int, int] = (1200, 180),
        latency_ms: int = 240,
    ):
        if answers is None:
            answers = []
        elif isinstance(answers, (dict, Exception)) or callable(answers):
            answers = [answers]
        self.answers = list(answers)
        self.model = model
        self.embedder = embedder  # callable(texts) -> vectors, or an Exception to raise
        self.tokens = tokens
        self.latency_ms = latency_ms
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.schemas: list[dict] = []
        self.embedded: list[list[str]] = []
        self.closed = 0

    @property
    def calls(self) -> int:
        return len(self.prompts)

    def complete(self, prompt: str, schema: dict, *, system: str = "") -> Completion:
        self.prompts.append(prompt)
        self.systems.append(system)
        self.schemas.append(schema)
        if not self.answers:
            raise AssertionError("FakeLLM was called but has no canned answer")
        answer = self.answers[min(len(self.prompts) - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer(prompt)
        return Completion(
            data=copy.deepcopy(answer),
            model=self.model,
            tokens_in=self.tokens[0],
            tokens_out=self.tokens[1],
            latency_ms=self.latency_ms,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.append(list(texts))
        if isinstance(self.embedder, Exception):
            raise self.embedder
        if self.embedder is None:
            return []
        return [[float(x) for x in vector] for vector in self.embedder(list(texts))]

    def close(self) -> None:
        self.closed += 1
