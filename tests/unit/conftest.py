"""Фикстуры читающего слоя (этап 1.3).

Корпус собирается на файловой базе под `hub_paths`: те же строки видит и
`FeedService`, и `TestClient` — он открывает собственное соединение к тому же
файлу. Ни один запрос не уходит в сеть и не зовёт модель.
"""

from __future__ import annotations

import itertools

import pytest

from src.models import EntitySpan, RawDocument, Source
from src.services.feed_service import FeedService

NOW = "2026-09-05T09:00:00+00:00"
# Расписание по умолчанию — заведомо в будущем: иначе `status()` объявил бы
# каждый источник просроченным по настенным часам, и тесты стали бы плавать.
NEVER_DUE = "2026-12-31T00:00:00+00:00"

# Тексты оригиналов: слово живёт только в одном из них, поэтому по нему видно,
# из какого источника поисковая строка собралась.
NPA_ORIGINAL = (
    "Минцифры внесло в правительство законопроекта о регулировании оборота ИИ-сервисов.\n"
    "Документ обязывает операторов проходить аккредитацию с 1 января 2027 года."
)
NEWS_ORIGINAL = (
    "Оператор платного ТВ запустил бета-версию рекомендательного сервиса.\n"
    "Подключение абонентов спутникового вещания обещано в течение квартала."
)


@pytest.fixture
def feed(config, file_db) -> FeedService:
    """Читающий слой поверх файловой базы — то же, что собирает и CLI, и API."""
    return FeedService(config, file_db)


@pytest.fixture
def source_factory(file_db):
    """`source_factory("Ведомости", category="media")` → сохранённый источник."""
    counter = itertools.count(1)

    def make(name: str, **fields) -> Source:
        index = next(counter)
        base = {
            "name": name,
            "url": f"https://s{index}.example.ru/",
            "kind": "rss",
            "category": "media",
            "fetch_url": f"https://s{index}.example.ru/rss",
            "next_run_at": NEVER_DUE,
        }
        return file_db.sources.add(Source(**{**base, **fields}))

    return make


@pytest.fixture
def document_factory(file_db):
    """Собранный документ без карточки — то, что показывает `/documents`."""
    counter = itertools.count(1)

    def make(
        source: Source,
        *,
        title="Документ",
        text="",
        published_at=NOW,
        fetched_at=NOW,
        hidden=0,
        last_error="",
    ) -> int:
        external_id = f"free-{next(counter)}"
        with file_db.transaction():
            document_id = file_db.documents.insert(
                RawDocument(
                    source_id=source.id,
                    external_id=external_id,
                    url=f"{source.url}{external_id}",
                    title=title,
                    text=text,
                    published_at=published_at,
                    fetched_at=fetched_at,
                )
            )
            if hidden:
                file_db.conn.execute(
                    "UPDATE documents SET hidden=1 WHERE id=?", (document_id,)
                )
            if last_error:
                file_db.documents.mark_failed(document_id, last_error, at=fetched_at)
        return document_id

    return make


@pytest.fixture
def card_factory(file_db, item_factory):
    """Документ, карточка, теги, сущности и строка поискового индекса — одним вызовом.

    Индекс наполняется тем же `SearchRepo.rebuild`, что и обработка, поэтому
    поисковые тесты видят ровно то, что увидит установка.
    """
    counter = itertools.count(1)

    def make(
        source: Source,
        *,
        title="Заголовок",
        summary="",
        original="",
        tags=(),
        entities=(),
        published_at=NOW,
        url=None,
        **item_fields,
    ) -> int:
        external_id = f"doc-{next(counter)}"
        with file_db.transaction():
            document_id = file_db.documents.insert(
                RawDocument(
                    source_id=source.id,
                    external_id=external_id,
                    url=url or f"{source.url}{external_id}",
                    title=title,
                    text=original,
                    published_at=published_at,
                    fetched_at=NOW,
                )
            )
            file_db.documents.set_derived(document_id, norm_text=original)
        item_id = item_factory(
            file_db,
            document_id,
            title=title,
            summary=summary,
            tags=list(tags),
            published_at=published_at,
            **item_fields,
        )
        with file_db.transaction():
            file_db.tags.set_tags(item_id, tags)
            file_db.items.add_entities(
                item_id, [EntitySpan(role=role, value=value) for role, value in entities]
            )
            file_db.search.rebuild(item_id)
        return item_id

    return make


@pytest.fixture
def corpus_sources(source_factory) -> dict[str, Source]:
    return {
        "media": source_factory("Ведомости", category="media"),
        "regulator": source_factory("Банк России", category="regulator"),
        "channel": source_factory("Канал ЦИТ", kind="telegram", category="telegram"),
    }


@pytest.fixture
def corpus(card_factory, corpus_sources) -> dict[str, int]:
    """Восемь карточек, покрывающих каждое состояние видимости и порядок сортировки.

    | ключ            | дата (UTC)  | тип  | приоритет | видимость      | источник  |
    |-----------------|-------------|------|-----------|----------------|-----------|
    | npa_high        | 09-04 09:00 | npa  | high      | visible        | media     |
    | news_medium     | 09-03 10:00 | news | medium    | visible        | regulator |
    | news_low        | 09-02 08:00 | news | low       | visible        | media     |
    | npa_medium      | 09-01 07:00 | npa  | medium    | visible        | regulator |
    | undated         | —           | news | low       | visible        | channel   |
    | hidden_digest   | 09-04 11:00 | news | high      | hidden_digest  | media     |
    | hidden_feed     | 09-04 12:00 | news | high      | hidden_feed    | media     |
    | deleted         | 09-04 13:00 | news | low       | deleted        | media     |
    """
    media = corpus_sources["media"]
    regulator = corpus_sources["regulator"]
    channel = corpus_sources["channel"]
    return {
        "npa_high": card_factory(
            media,
            title="Минцифры внесло законопроект об аккредитации ИИ-сервисов",
            summary="Операторы ИИ обязаны пройти аккредитацию.",
            original=NPA_ORIGINAL,
            tags=("регуляторика",),
            entities=(("who", "Минцифры"), ("act_number", "112233-8")),
            published_at="2026-09-04T09:00:00+00:00",
            processed_at="2026-09-04T09:30:00+00:00",
            type="npa",
            priority="high",
            npa_status="внесён",
            npa_key="112233-8",
        ),
        "news_medium": card_factory(
            regulator,
            title="Оператор платного ТВ запустил рекомендательный сервис",
            summary="Бета-версия работает на собственных моделях.",
            original=NEWS_ORIGINAL,
            tags=("конкуренты", "регуляторика"),
            published_at="2026-09-03T10:00:00+00:00",
            processed_at="2026-09-03T10:30:00+00:00",
            priority="medium",
        ),
        "news_low": card_factory(
            media,
            title="Обзор рынка спутникового вещания",
            summary="Аналитики ждут консолидации.",
            tags=("тренды",),
            published_at="2026-09-02T08:00:00+00:00",
            processed_at="2026-09-05T08:00:00+00:00",
            priority="low",
        ),
        "npa_medium": card_factory(
            regulator,
            title="Комитет вернулся к рассмотрению поправок",
            tags=("регуляторика",),
            published_at="2026-09-01T07:00:00+00:00",
            processed_at="2026-09-01T07:30:00+00:00",
            type="npa",
            priority="medium",
            npa_status="рассмотрение",
        ),
        "undated": card_factory(
            channel,
            title="Пост без даты публикации",
            published_at=None,
            processed_at="2026-09-05T07:00:00+00:00",
            priority="low",
            date_estimated=True,
        ),
        "hidden_digest": card_factory(
            media,
            title="Скрыто из дайджеста, но не из ленты",
            tags=("конкуренты",),
            published_at="2026-09-04T11:00:00+00:00",
            processed_at="2026-09-04T11:30:00+00:00",
            priority="high",
            visibility="hidden_digest",
            hidden_reason="не для инфраструктуры",
        ),
        "hidden_feed": card_factory(
            media,
            title="Скрыто из ленты",
            published_at="2026-09-04T12:00:00+00:00",
            processed_at="2026-09-04T12:30:00+00:00",
            priority="high",
            visibility="hidden_feed",
            hidden_reason="нерелевантно",
        ),
        "deleted": card_factory(
            media,
            title="Мягко удалённая карточка",
            published_at="2026-09-04T13:00:00+00:00",
            processed_at="2026-09-04T13:30:00+00:00",
            priority="low",
            visibility="deleted",
        ),
    }
