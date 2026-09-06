"""src/services/source_service.py — probe, create, pause, soft delete, restore, health.

Every HTTP call goes through `MockRoutes`: `SourceService` builds its client with
`make_client`, so that one name is what the tests replace. Nothing here reaches
the network and nothing reads the wall clock.
"""

from __future__ import annotations

import httpx
import pytest
from support import HTML_UTF8, RSS, MockRoutes, raising, rss_bytes

from src.exceptions import SourceError, SourceExistsError
from src.models import KINDS, POLL_INTERVALS, FetchState, RawDocument, Resolution, Source
from src.services import source_service as manage
from src.services.source_service import SourceService
from src.sources.base import make_client

NOW = "2026-09-02T12:00:00+00:00"
PROBLEM = "application/problem+json"

FEED_URL = "https://feed.example.ru/rss.xml"
SITE_URL = "https://www.cableman.ru/"
SITE_FEED_URL = "https://www.cableman.ru/rss/"
TG_URL = "https://t.me/cit_gov"
TG_PREVIEW_URL = "https://t.me/s/cit_gov"

FEED_ITEMS = [
    {
        "title": "Минцифры внесло законопроект об аккредитации ИИ-сервисов",
        "link": "https://feed.example.ru/news/1",
        "pubDate": "Tue, 02 Sep 2026 09:00:00 +0300",
    },
    {
        "title": "Операторы ИИ получат отсрочку",
        "link": "https://feed.example.ru/news/2",
        "pubDate": "Mon, 01 Sep 2026 09:00:00 +0300",
    },
    {
        "title": "Реестр отечественного ПО пополнился",
        "link": "https://feed.example.ru/news/3",
        "pubDate": "Sun, 31 Aug 2026 09:00:00 +0300",
    },
]

HOME_WITH_FEED = (
    "<!DOCTYPE html><html><head><title>Кабельщик</title>"
    '<link rel="alternate" type="application/rss+xml" title="RSS" href="/rss/">'
    "</head><body><p>привет</p></body></html>"
).encode("utf-8")


@pytest.fixture
def routes() -> MockRoutes:
    """The whole internet as far as `SourceService` is concerned."""
    return MockRoutes()


@pytest.fixture
def service(monkeypatch, config, db, routes, frozen_clock) -> SourceService:
    monkeypatch.setattr(
        manage,
        "make_client",
        lambda cfg, transport=None: make_client(cfg, routes.transport()),
    )
    return SourceService(config, db)


def _feed_route(routes: MockRoutes, url: str = FEED_URL, items=None) -> None:
    body = rss_bytes(FEED_ITEMS if items is None else items, title="Отраслевые новости")
    routes[url] = (200, body, RSS)


def _stub_resolver(monkeypatch, resolution: Resolution) -> None:
    """Pin the resolver's answer when the case under test is not about resolving."""

    class StubResolver:
        def __init__(self, client, limiter=None, probe_timeout=8.0):
            pass

        def resolve(self, url):
            return resolution

    monkeypatch.setattr(manage, "Resolver", StubResolver)


def _add(db, **overrides) -> Source:
    base = dict(name="Лента", url="https://a.ru/", kind="rss", category="media",
                fetch_url="https://a.ru/rss")
    return db.sources.add(Source(**{**base, **overrides}))


def _document(db, source_id: int, external_id: str) -> int:
    with db.transaction():
        return db.documents.insert(
            RawDocument(
                source_id=source_id,
                external_id=external_id,
                url=f"https://a.ru/{external_id}",
                title=f"Док {external_id}",
                text="Текст",
                fetched_at="2026-09-02T12:00:00+00:00",
            )
        )


# ── probe: direct feed ─────────────────────────────────────────────────────


def test_probe_of_a_direct_feed_reports_the_type_title_and_preview(service, routes):
    _feed_route(routes)

    result = service.probe(FEED_URL)

    assert result.resolved_type == "rss"
    assert result.feed_url == FEED_URL
    assert result.title == "Отраслевые новости"
    assert result.detection_method == "direct"
    assert [row["title"] for row in result.preview] == [i["title"] for i in FEED_ITEMS]
    assert result.preview[0]["url"] == "https://feed.example.ru/news/1"
    assert result.preview[0]["published_at"] == "2026-09-02T06:00:00+00:00"
    assert result.warnings == []


def test_probe_shows_at_most_five_materials(service, routes):
    _feed_route(routes, items=[{"title": f"Новость {i}", "link": f"https://feed.example.ru/{i}"}
                               for i in range(9)])
    assert len(service.probe(FEED_URL).preview) == manage.PREVIEW_LIMIT == 5


def test_probe_writes_nothing_to_the_database(service, routes, db):
    _feed_route(routes)
    service.probe(FEED_URL)
    assert db.sources.list(include_deleted=True) == []
    assert db.documents.count() == 0


def test_probe_suggests_six_hours_for_media_and_an_hour_for_a_regulator(service, routes,
                                                                        monkeypatch):
    _feed_route(routes)
    assert service.probe(FEED_URL).suggested_poll_interval == "6h"

    gov_feed = "https://digital.gov.ru/rss/"
    _feed_route(routes, url=gov_feed)
    assert service.probe(gov_feed).suggested_poll_interval == "1h"


def test_probe_of_an_empty_feed_warns_that_it_is_empty(service, routes, monkeypatch):
    """Лента разбирается, но материалов в ней нет.

    Резолвер сам по себе пустую ленту лентой не признает, поэтому тип подставлен:
    проверяется именно предупреждение превью, а не распознавание.
    """
    _feed_route(routes, items=[])
    _stub_resolver(monkeypatch, Resolution(kind="rss", fetch_url=FEED_URL, name="Пустая"))

    result = service.probe(FEED_URL)

    assert result.preview == []
    assert result.warnings == ["empty_feed"]


def test_probe_warns_when_no_material_carries_a_date(service, routes):
    _feed_route(routes, items=[{"title": "Без даты", "link": "https://feed.example.ru/x"}])
    result = service.probe(FEED_URL)
    assert [row["published_at"] for row in result.preview] == [None]
    assert result.warnings == ["no_dates"]


def test_probe_without_preview_asks_the_feed_only_once(service, routes):
    _feed_route(routes)
    result = service.probe(FEED_URL, with_preview=False)
    assert result.resolved_type == "rss"
    assert result.preview == []
    assert routes.urls() == [FEED_URL]


# ── probe: discovery, telegram, failures ───────────────────────────────────


def test_probe_finds_the_feed_advertised_by_link_rel_alternate(service, routes):
    routes[SITE_URL] = (200, HOME_WITH_FEED, HTML_UTF8)
    _feed_route(routes, url=SITE_FEED_URL)

    result = service.probe(SITE_URL)

    assert result.resolved_type == "rss"
    assert result.feed_url == SITE_FEED_URL
    assert result.detection_method == "resolver"
    assert result.note == f"feed discovered at {SITE_FEED_URL}"
    assert len(result.preview) == 3


def test_probe_of_a_telegram_channel_uses_the_web_preview(service, routes, fixture_bytes):
    routes[TG_PREVIEW_URL] = (200, fixture_bytes("tg_channel.html"), HTML_UTF8)

    result = service.probe(TG_URL)

    assert result.resolved_type == "telegram"
    assert result.feed_url == TG_PREVIEW_URL
    assert result.title == "cit_gov"
    assert result.suggested_poll_interval == "1h"
    # В фикстуре канала четыре читаемых поста — меньше потолка превью.
    assert 0 < len(result.preview) <= manage.PREVIEW_LIMIT
    assert all(row["url"].startswith("https://t.me/cit_gov/") for row in result.preview)


def test_probe_of_a_channel_without_a_preview_says_so(service, routes):
    routes[TG_PREVIEW_URL] = (404, b"nope", HTML_UTF8)
    result = service.probe(TG_URL)
    assert result.resolved_type == "telegram"
    assert result.warnings == ["telegram_preview_unavailable"]
    assert result.preview == []


def test_probe_of_an_unreachable_host_degrades_to_html_and_warns(service, routes):
    routes[SITE_URL] = raising(httpx.ConnectError("nope"))
    result = service.probe(SITE_URL)
    assert result.resolved_type == "html"
    assert result.note.startswith("unreachable now:")
    assert result.warnings == ["network_unreachable"]


def test_probe_of_a_paywalled_host_warns_before_anything_is_stored(service, routes):
    paywalled = "https://www.vedomosti.ru/rss/news"
    _feed_route(routes, url=paywalled)
    assert service.probe(paywalled).warnings == ["paywall_suspected"]


def test_probe_of_a_non_http_link_is_kept_as_manual_without_a_preview(service, routes):
    result = service.probe("ftp://archive.example.ru/dump")
    assert result.resolved_type == "manual"
    assert result.preview == []
    assert result.note == "ftp:// is not fetched; kept as manual"
    assert routes.requests == []


def test_probe_marks_an_unsupported_resolution_with_a_warning(service, monkeypatch):
    _stub_resolver(monkeypatch, Resolution(kind="unsupported", fetch_url="", note="ничего не нашли"))
    result = service.probe("https://intranet.example.ru/")
    assert result.resolved_type == "unsupported"
    assert result.warnings == ["unsupported_source"]


@pytest.mark.parametrize("url", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
def test_probe_of_a_blank_url_is_a_validation_error(service, url):
    with pytest.raises(SourceError, match="пустая ссылка") as info:
        service.probe(url)
    assert info.value.code == "validation_error"


def test_probe_reports_that_the_same_address_is_already_a_source(service, routes, db):
    existing = _add(db, name="РФРИТ", url=TG_URL, kind="telegram", category="telegram",
                    fetch_url=TG_PREVIEW_URL)
    routes[TG_PREVIEW_URL] = (200, b"<html><body></body></html>", HTML_UTF8)

    result = service.probe("https://t.me/cit_gov/", with_preview=False)

    assert result.already_exists is True
    assert result.already_exists_source_id == existing.id


def test_probe_of_a_deleted_source_address_reports_it_as_free(service, routes, db):
    existing = _add(db)
    db.sources.remove(existing.id)
    _feed_route(routes, url="https://a.ru/rss")

    result = service.probe("https://a.ru/rss", with_preview=False)

    assert result.already_exists is False
    assert result.already_exists_source_id is None


# ── create ─────────────────────────────────────────────────────────────────


def test_create_with_a_pinned_kind_stores_an_active_scheduled_source(service, db, routes):
    source = service.create(
        "https://a.ru/", title="Лента А", kind="rss", fetch_url="https://a.ru/rss"
    )

    assert routes.requests == []  # a pinned kind means no resolution round trip
    stored = db.sources.get(source.id)
    assert (stored.name, stored.kind, stored.category) == ("Лента А", "rss", "media")
    assert stored.status == "active"
    assert stored.normalized_url == "a.ru/rss"
    assert stored.poll_interval == "1h"
    assert stored.next_run_at == "2026-09-02T12:00:00+00:00"
    assert stored.created_at == "2026-09-02T12:00:00+00:00"


def test_create_resolves_the_url_when_the_kind_is_not_pinned(service, db, routes):
    routes[SITE_URL] = (200, HOME_WITH_FEED, HTML_UTF8)
    _feed_route(routes, url=SITE_FEED_URL)

    source = service.create(SITE_URL)

    assert (source.kind, source.fetch_url) == ("rss", SITE_FEED_URL)
    assert source.name == "Отраслевые новости"
    assert source.poll_interval == "6h"  # the media default the probe suggested
    assert db.sources.get_by_normalized("cableman.ru/rss").id == source.id


def test_create_records_the_category_hint_and_the_author(service, db):
    source = service.create(
        "https://sozd.duma.gov.ru/", kind="html", fetch_url="https://sozd.duma.gov.ru/",
        category_hint="npa", created_by="аналитик",
    )
    stored = db.sources.get(source.id)
    assert (stored.category, stored.category_hint) == ("regulator", "npa")
    assert stored.created_by == "аналитик"


def test_create_rejects_an_address_that_is_already_a_source(service, db):
    existing = _add(db, name="РФРИТ", url=TG_URL, kind="telegram", category="telegram",
                    fetch_url=TG_PREVIEW_URL)

    with pytest.raises(SourceError, match="уже добавлен как источник") as info:
        service.create("https://t.me/cit_gov/", kind="telegram", fetch_url=TG_URL)

    assert info.value.code == "source_exists"
    assert info.value.details == {"source_id": existing.id}
    assert len(db.sources.list()) == 1


def test_adding_a_deleted_address_again_revives_the_source(service, db):
    """Собранное осталось на старом id, поэтому адрес возвращает источник, а не дублирует."""
    first = _add(db)
    db.sources.remove(first.id)

    second = service.create("https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")

    assert second.id == first.id
    assert second.status == "active"
    assert [s.id for s in db.sources.list()] == [first.id]


@pytest.mark.parametrize("interval", ["30m", "hour", "1H", ""], ids=["half-hour", "word",
                                                                     "wrong-case", "blank-is-ok"])
def test_create_validates_the_polling_interval(service, db, interval):
    if interval == "":
        assert service.create("https://a.ru/", kind="rss", fetch_url="https://a.ru/rss",
                              poll_interval=interval).poll_interval == "1h"
        return
    with pytest.raises(SourceError, match="periodicity must be one of") as info:
        service.create("https://a.ru/", kind="rss", fetch_url="https://a.ru/rss",
                       poll_interval=interval)
    assert info.value.code == "validation_error"
    assert db.sources.list() == []


@pytest.mark.parametrize("interval", POLL_INTERVALS)
def test_create_accepts_every_interval_of_the_vocabulary(service, db, interval):
    source = service.create("https://a.ru/", kind="rss", fetch_url="https://a.ru/rss",
                            poll_interval=interval)
    assert db.sources.get(source.id).poll_interval == interval


def test_create_refuses_an_address_nothing_can_poll(service, db, monkeypatch):
    _stub_resolver(monkeypatch, Resolution(kind="unsupported", fetch_url="", note="ничего не нашли"))
    with pytest.raises(SourceError, match="не удалось определить, как опрашивать") as info:
        service.create("https://intranet.example.ru/")
    assert info.value.code == "unsupported_source"
    assert info.value.details == {"note": "ничего не нашли"}
    assert db.sources.list() == []


# ── read ───────────────────────────────────────────────────────────────────


def test_get_of_a_missing_source_is_a_named_error(service):
    with pytest.raises(SourceError, match="источник #42 не найден") as info:
        service.get(42)
    assert info.value.code == "source_not_found"


def test_list_hides_deleted_sources_and_can_show_only_them(service, db):
    live = _add(db, name="Живой")
    gone = _add(db, name="Удалённый", fetch_url="https://b.ru/rss")
    db.sources.remove(gone.id)

    assert [s.id for s in service.list()] == [live.id]
    assert [s.id for s in service.list(status="deleted")] == [gone.id]
    assert [s.id for s in service.list(status="active")] == [live.id]


def test_list_filters_by_kind(service, db):
    rss = _add(db)
    _add(db, name="Канал", kind="telegram", fetch_url=TG_PREVIEW_URL)
    assert [s.id for s in service.list(kind="rss")] == [rss.id]


def test_list_rejects_a_status_outside_the_vocabulary(service):
    with pytest.raises(SourceError, match="status must be one of") as info:
        service.list(status="выключен")
    assert info.value.code == "validation_error"


# ── update / pause / resume ────────────────────────────────────────────────


def test_changing_the_interval_moves_the_next_poll_immediately(service, db):
    source = service.create("https://a.ru/", kind="rss", fetch_url="https://a.ru/rss",
                            poll_interval="24h")
    db.sources.schedule(source.id, "2026-09-03T12:00:00+00:00")

    updated = service.update(source.id, poll_interval="15m")

    assert updated.poll_interval == "15m"
    # not "at the end of the current day" — a quarter of an hour from now
    assert db.sources.get(source.id).next_run_at == "2026-09-02T12:15:00+00:00"


def test_update_renames_without_touching_the_schedule(service, db):
    source = service.create("https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    db.sources.schedule(source.id, "2026-09-03T12:00:00+00:00")

    updated = service.update(source.id, name="Новое имя")

    assert updated.name == "Новое имя"
    assert db.sources.get(source.id).next_run_at == "2026-09-03T12:00:00+00:00"


def test_update_clears_the_category_hint_with_an_empty_string(service, db):
    source = service.create("https://a.ru/", kind="rss", fetch_url="https://a.ru/rss",
                            category_hint="npa")
    assert service.update(source.id, category_hint="").category_hint is None
    assert db.sources.get(source.id).category_hint is None


def test_pause_takes_the_source_out_of_the_rotation_and_resume_puts_it_back(service, db):
    source = service.create("https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")

    assert service.pause(source.id).status == "paused"
    assert db.sources.due("2026-09-02T12:00:00+00:00") == []

    assert service.resume(source.id).status == "active"
    assert [s.id for s in db.sources.due("2026-09-02T12:00:00+00:00")] == [source.id]


def test_update_rejects_a_status_outside_the_vocabulary(service, db):
    source = service.create("https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    with pytest.raises(SourceError, match="status must be one of") as info:
        service.update(source.id, status="выключен")
    assert info.value.code == "validation_error"
    assert db.sources.get(source.id).status == "active"


@pytest.mark.parametrize("interval", ["30m", "1 hour"])
def test_update_rejects_an_unknown_interval_and_changes_nothing(service, db, interval):
    source = service.create("https://a.ru/", kind="rss", fetch_url="https://a.ru/rss",
                            poll_interval="6h")
    with pytest.raises(SourceError, match="periodicity must be one of"):
        service.update(source.id, poll_interval=interval)
    assert db.sources.get(source.id).poll_interval == "6h"


def test_update_of_a_missing_source_is_a_named_error(service):
    with pytest.raises(SourceError) as info:
        service.update(42, name="x")
    assert info.value.code == "source_not_found"


# ── update: переезд на другой адрес ────────────────────────────────────────


def _relocatable(service) -> Source:
    return service.create("https://a.ru/", title="Лента А", kind="rss", fetch_url="https://a.ru/rss")


def _address(db, source_id: int) -> tuple[str, str, str, str]:
    stored = db.sources.get(source_id)
    return stored.url, stored.kind, stored.fetch_url, stored.normalized_url


def test_update_with_a_pinned_kind_and_fetch_url_moves_the_source_without_a_network_call(
    service, db, routes
):
    source = _relocatable(service)
    db.sources.schedule(source.id, "2026-09-03T12:00:00+00:00")

    updated = service.update(source.id, url="https://b.ru/", kind="rss", fetch_url="https://b.ru/feed")

    assert routes.requests == []
    assert updated.id == source.id
    assert _address(db, source.id) == ("https://b.ru/", "rss", "https://b.ru/feed", "b.ru/feed")
    # Новый адрес опрашивается сразу, а не в конце текущего периода.
    assert db.sources.get(source.id).next_run_at == NOW


def test_update_of_the_url_alone_probes_the_new_address(service, db, routes):
    source = _relocatable(service)
    routes[SITE_URL] = (200, HOME_WITH_FEED, HTML_UTF8)
    _feed_route(routes, url=SITE_FEED_URL)

    updated = service.update(source.id, url=SITE_URL)

    assert SITE_URL in routes.urls()
    assert (updated.kind, updated.fetch_url, updated.normalized_url) == (
        "rss", SITE_FEED_URL, "cableman.ru/rss"
    )
    assert _address(db, source.id) == (SITE_URL, "rss", SITE_FEED_URL, "cableman.ru/rss")


def test_update_to_an_address_nothing_can_poll_is_refused_and_changes_nothing(
    service, db, monkeypatch
):
    source = _relocatable(service)
    _stub_resolver(monkeypatch, Resolution(kind="unsupported", fetch_url="", note="ничего не нашли"))

    with pytest.raises(SourceError, match="не удалось определить, как опрашивать") as info:
        service.update(source.id, url="https://intranet.example.ru/")

    assert info.value.code == "unsupported_source"
    assert info.value.details == {"note": "ничего не нашли"}
    assert _address(db, source.id) == ("https://a.ru/", "rss", "https://a.ru/rss", "a.ru/rss")


def test_update_to_the_address_of_another_live_source_is_a_conflict(service, db):
    first = _relocatable(service)
    other = service.create("https://b.ru/", kind="rss", fetch_url="https://b.ru/feed")

    with pytest.raises(SourceError, match="уже добавлен как источник") as info:
        service.update(first.id, url="https://b.ru/", kind="rss", fetch_url="https://b.ru/feed")

    assert info.value.code == "source_exists"
    assert info.value.details == {"source_id": other.id}
    assert _address(db, first.id) == ("https://a.ru/", "rss", "https://a.ru/rss", "a.ru/rss")


def test_update_to_the_address_of_a_deleted_source_is_a_conflict_not_a_crash(service, db):
    """Как и при создании (`_revive`): удалённый адрес возвращают через restore, а не занимают.

    `sources.fetch_url` уникален и для удалённых строк, поэтому без этой проверки
    переезд падал бы в репозитории с IntegrityError.
    """
    first = _relocatable(service)
    gone = service.create("https://b.ru/", kind="rss", fetch_url="https://b.ru/feed")
    service.soft_delete(gone.id)

    with pytest.raises(SourceError, match="restore") as info:
        service.update(first.id, url="https://b.ru/", kind="rss", fetch_url="https://b.ru/feed")

    assert (info.value.code, info.value.status_code) == ("source_exists", 409)
    assert info.value.details == {"source_id": gone.id, "status": "deleted"}
    assert f"#{gone.id}" in info.value.message
    assert _address(db, first.id) == ("https://a.ru/", "rss", "https://a.ru/rss", "a.ru/rss")
    assert db.sources.get(gone.id).status == "deleted"


def test_update_to_a_deleted_address_spelled_differently_is_the_same_conflict(service, db):
    """Удалённый источник узнаётся и по нормализованному адресу, не только по точному `fetch_url`."""
    first = _relocatable(service)
    gone = service.create("https://b.ru/", kind="rss", fetch_url="https://b.ru/feed")
    service.soft_delete(gone.id)

    with pytest.raises(SourceError) as info:
        service.update(first.id, url="http://www.b.ru/", kind="rss", fetch_url="http://www.b.ru/feed/")

    assert info.value.details == {"source_id": gone.id, "status": "deleted"}


@pytest.mark.parametrize(
    "fields",
    [
        {"url": "https://a.ru/"},
        {"kind": "rss"},
        {"url": "https://a.ru/", "kind": "rss"},
    ],
    ids=["same-url", "same-kind", "same-url-and-kind"],
)
def test_update_with_the_unchanged_address_makes_no_probe_and_keeps_the_cursor(
    service, db, routes, fields
):
    """Переезд «туда же» — не переезд: ни запроса в сеть, ни сброса состояния опроса."""
    source = _relocatable(service)
    db.sources.schedule(source.id, "2026-09-03T12:00:00+00:00")
    with db.transaction():
        db.fetch_state.save(FetchState(source_id=source.id, etag='"abc"', cursor={"after": 42}))

    service.update(source.id, **fields)

    assert routes.requests == []
    assert _address(db, source.id) == ("https://a.ru/", "rss", "https://a.ru/rss", "a.ru/rss")
    assert db.sources.get(source.id).next_run_at == "2026-09-03T12:00:00+00:00"
    state = db.fetch_state.get(source.id)
    assert (state.etag, state.cursor) == ('"abc"', {"after": 42})


def test_update_with_the_unchanged_url_but_another_kind_still_probes(service, db, routes):
    """Смена типа при старом адресе — это переезд: резолвер должен подтвердить, чем опрашивать."""
    source = _relocatable(service)
    routes["https://a.ru/"] = (200, HOME_WITH_FEED, HTML_UTF8)
    _feed_route(routes, url="https://a.ru/rss/")

    service.update(source.id, kind="html")

    assert "https://a.ru/" in routes.urls()
    assert db.sources.get(source.id).kind == "html"


def test_update_with_the_sources_own_address_is_not_a_conflict_and_changes_nothing(service, db):
    source = _relocatable(service)
    db.sources.schedule(source.id, "2026-09-03T12:00:00+00:00")

    service.update(source.id, url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")

    assert _address(db, source.id) == ("https://a.ru/", "rss", "https://a.ru/rss", "a.ru/rss")
    assert db.sources.get(source.id).next_run_at == "2026-09-03T12:00:00+00:00"


@pytest.mark.parametrize("url", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
def test_update_to_an_empty_url_is_a_validation_error(service, db, url):
    source = _relocatable(service)

    with pytest.raises(SourceError, match="пустая ссылка") as info:
        service.update(source.id, url=url)

    assert info.value.code == "validation_error"
    assert _address(db, source.id) == ("https://a.ru/", "rss", "https://a.ru/rss", "a.ru/rss")


@pytest.mark.parametrize("kind", ["atom", "RSS", "feed", ""], ids=["atom", "wrong-case", "word", "empty"])
def test_update_rejects_a_kind_outside_the_vocabulary(service, db, routes, kind):
    source = _relocatable(service)

    with pytest.raises(SourceError, match="type must be one of") as info:
        service.update(source.id, url="https://b.ru/", kind=kind, fetch_url="https://b.ru/feed")

    assert info.value.code == "validation_error"
    assert routes.requests == []
    assert _address(db, source.id) == ("https://a.ru/", "rss", "https://a.ru/rss", "a.ru/rss")


@pytest.mark.parametrize("kind", KINDS)
def test_update_accepts_every_kind_of_the_vocabulary_when_the_fetch_url_is_pinned(service, db, kind):
    source = _relocatable(service)

    updated = service.update(source.id, url="https://b.ru/", kind=kind, fetch_url="https://b.ru/x")

    assert updated.kind == kind
    assert db.sources.get(source.id).kind == kind


def test_a_real_move_resets_the_cursor_and_validators_but_keeps_the_failure_history(service, db):
    source = _relocatable(service)
    with db.transaction():
        db.fetch_state.save(
            FetchState(
                source_id=source.id, etag='"abc"', last_modified="Tue, 02 Sep 2026 09:00:00 GMT",
                last_success_at=NOW, last_error="HTTP 503", consecutive_failures=2,
                cursor={"after": 42},
            )
        )

    service.update(source.id, url="https://b.ru/", kind="rss", fetch_url="https://b.ru/feed")

    state = db.fetch_state.get(source.id)
    assert (state.etag, state.last_modified, state.cursor) == (None, None, {})
    assert (state.consecutive_failures, state.last_error, state.last_success_at) == (
        2, "HTTP 503", NOW
    )


def test_an_unchanged_address_leaves_the_cursor_alone(service, db):
    source = _relocatable(service)
    with db.transaction():
        db.fetch_state.save(FetchState(source_id=source.id, etag='"abc"', cursor={"after": 42}))

    service.update(source.id, url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")

    state = db.fetch_state.get(source.id)
    assert (state.etag, state.cursor) == ('"abc"', {"after": 42})


@pytest.mark.parametrize(
    ("url", "kind", "fetch_url", "category"),
    [
        ("https://t.me/cit_gov", "telegram", "https://t.me/s/cit_gov", "telegram"),
        ("https://sozd.duma.gov.ru/", "html", "https://sozd.duma.gov.ru/", "regulator"),
        ("ftp://archive.example.ru/", "manual", "ftp://archive.example.ru/", "manual"),
    ],
    ids=["telegram", "regulator-html", "manual"],
)
def test_the_category_follows_a_changed_kind(service, db, url, kind, fetch_url, category):
    source = _relocatable(service)
    assert source.category == "media"

    updated = service.update(source.id, url=url, kind=kind, fetch_url=fetch_url)

    assert updated.category == category
    assert db.sources.get(source.id).category == category


def test_the_category_is_kept_when_only_the_address_changes(service, db):
    """Категория пересчитывается только вместе с типом: переезд ленты её не трогает."""
    source = _relocatable(service)

    updated = service.update(
        source.id, url="https://cbr.ru/", kind="rss", fetch_url="https://cbr.ru/rss"
    )

    assert updated.category == "media"


def test_documents_keep_their_source_id_after_the_move(service, db):
    source = _relocatable(service)
    ids = [_document(db, source.id, f"d{i}") for i in range(2)]

    service.update(source.id, url="https://b.ru/", kind="rss", fetch_url="https://b.ru/feed")

    assert db.documents.count(source.id) == 2
    assert [db.documents.get(i).source_id for i in ids] == [source.id, source.id]
    assert [s.id for s in db.sources.list()] == [source.id]


def test_update_can_move_and_rename_in_one_call(service, db):
    source = _relocatable(service)

    updated = service.update(
        source.id, name="Канал ЦИТ", url=TG_URL, kind="telegram", fetch_url=TG_PREVIEW_URL,
        poll_interval="15m",
    )

    stored = db.sources.get(source.id)
    assert (stored.name, stored.kind, stored.fetch_url, stored.poll_interval) == (
        "Канал ЦИТ", "telegram", TG_PREVIEW_URL, "15m"
    )
    assert updated.name == "Канал ЦИТ"


def test_relocating_a_missing_source_is_a_named_error(service):
    with pytest.raises(SourceError) as info:
        service.update(42, url="https://b.ru/", kind="rss", fetch_url="https://b.ru/feed")

    assert info.value.code == "source_not_found"


# ── PATCH /sources/{id}: переезд по HTTP ───────────────────────────────────


@pytest.fixture
def stored(file_db) -> Source:
    return file_db.sources.add(
        Source(
            name="Лента", url="https://a.ru/", kind="rss", category="media",
            fetch_url="https://a.ru/rss", normalized_url="a.ru/rss",
        )
    )


@pytest.fixture
def no_network(monkeypatch) -> None:
    """Резолвер сервиса не должен собирать клиент: пиннутый тип и адрес его не зовут."""

    def never(*args, **kwargs):
        raise AssertionError("резолвер не должен ходить в сеть")

    monkeypatch.setattr(manage, "make_client", never)


def test_patch_moves_the_source_to_a_pinned_address_without_a_network_call(
    client, file_db, stored, frozen_clock, no_network
):
    response = client.patch(
        f"/api/v1/sources/{stored.id}",
        json={"url": "https://b.ru/", "type": "rss", "fetch_url": "https://b.ru/feed"},
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["url"], body["kind"], body["fetch_url"], body["normalized_url"]) == (
        "https://b.ru/", "rss", "https://b.ru/feed", "b.ru/feed"
    )
    assert body["next_run_at"] == NOW
    assert file_db.sources.get(stored.id).fetch_url == "https://b.ru/feed"


def test_patch_changes_the_kind_and_the_category_together(client, stored, no_network):
    body = client.patch(
        f"/api/v1/sources/{stored.id}",
        json={"url": TG_URL, "type": "telegram", "fetch_url": TG_PREVIEW_URL},
    ).json()

    assert (body["kind"], body["category"], body["fetch_url"]) == (
        "telegram", "telegram", TG_PREVIEW_URL
    )


def test_patch_of_the_url_alone_resolves_it_through_the_network(client, stored, routes, monkeypatch):
    monkeypatch.setattr(
        manage, "make_client", lambda cfg, transport=None: make_client(cfg, routes.transport())
    )
    routes[SITE_URL] = (200, HOME_WITH_FEED, HTML_UTF8)
    _feed_route(routes, url=SITE_FEED_URL)

    body = client.patch(f"/api/v1/sources/{stored.id}", json={"url": SITE_URL}).json()

    assert (body["url"], body["kind"], body["fetch_url"]) == (SITE_URL, "rss", SITE_FEED_URL)


def test_patch_to_an_address_nothing_can_poll_is_a_422_problem(client, stored, monkeypatch):
    _stub_resolver(monkeypatch, Resolution(kind="unsupported", fetch_url="", note="ничего не нашли"))
    monkeypatch.setattr(manage, "make_client", lambda cfg, transport=None: MockRoutes().client())

    response = client.patch(f"/api/v1/sources/{stored.id}", json={"url": "https://intranet.example.ru/"})

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "unsupported_source"


def test_patch_to_the_address_of_another_source_is_a_409_problem(client, file_db, stored, no_network):
    other = file_db.sources.add(
        Source(name="Другая", url="https://b.ru/", kind="rss", fetch_url="https://b.ru/feed",
               normalized_url="b.ru/feed")
    )

    response = client.patch(
        f"/api/v1/sources/{stored.id}",
        json={"url": "https://b.ru/", "type": "rss", "fetch_url": "https://b.ru/feed"},
    )

    assert response.status_code == 409
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert (body["code"], body["details"]) == ("source_exists", {"source_id": other.id})
    assert file_db.sources.get(stored.id).fetch_url == "https://a.ru/rss"


@pytest.mark.parametrize(
    "payload",
    [
        {"url": ""},
        {"url": "   "},
        {"type": "atom"},
        {"url": "https://b.ru/", "type": "atom", "fetch_url": "https://b.ru/feed"},
    ],
    ids=["empty-url", "blank-url", "unknown-type", "unknown-type-with-address"],
)
def test_a_bad_relocation_is_a_400_problem_and_changes_nothing(
    client, file_db, stored, no_network, payload
):
    response = client.patch(f"/api/v1/sources/{stored.id}", json=payload)

    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "validation_error"
    unchanged = file_db.sources.get(stored.id)
    assert (unchanged.url, unchanged.kind, unchanged.fetch_url) == (
        "https://a.ru/", "rss", "https://a.ru/rss"
    )


def test_patch_with_the_unchanged_url_makes_no_network_call_and_changes_nothing(
    client, file_db, stored, no_network
):
    before = file_db.sources.get(stored.id).next_run_at

    response = client.patch(f"/api/v1/sources/{stored.id}", json={"url": "https://a.ru/"})

    assert response.status_code == 200
    body = response.json()
    assert (body["url"], body["kind"], body["fetch_url"], body["next_run_at"]) == (
        "https://a.ru/", "rss", "https://a.ru/rss", before
    )


def test_patch_to_the_address_of_a_deleted_source_is_a_409_pointing_at_it(
    client, file_db, stored, no_network
):
    gone = file_db.sources.add(
        Source(name="Удалённая", url="https://b.ru/", kind="rss", fetch_url="https://b.ru/feed",
               normalized_url="b.ru/feed")
    )
    file_db.sources.remove(gone.id)

    response = client.patch(
        f"/api/v1/sources/{stored.id}",
        json={"url": "https://b.ru/", "type": "rss", "fetch_url": "https://b.ru/feed"},
    )

    assert response.status_code == 409
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert (body["code"], body["details"]) == (
        "source_exists", {"source_id": gone.id, "status": "deleted"}
    )
    assert "restore" in body["detail"]
    assert file_db.sources.get(stored.id).fetch_url == "https://a.ru/rss"


def test_the_wire_name_of_the_kind_is_type_not_kind(client, stored, no_network):
    response = client.patch(f"/api/v1/sources/{stored.id}", json={"kind": "telegram"})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_patch_can_move_and_rename_in_one_request(client, file_db, stored, no_network):
    body = client.patch(
        f"/api/v1/sources/{stored.id}",
        json={"title": "Лента Б", "url": "https://b.ru/", "type": "rss",
              "fetch_url": "https://b.ru/feed"},
    ).json()

    assert (body["name"], body["url"]) == ("Лента Б", "https://b.ru/")
    assert file_db.sources.get(stored.id).name == "Лента Б"


# ── soft delete / restore ──────────────────────────────────────────────────


def test_soft_delete_keeps_the_documents_and_reports_how_many(service, db):
    source = _add(db)
    for i in range(3):
        _document(db, source.id, f"d{i}")

    report = service.soft_delete(source.id)

    assert report == {
        "source_id": source.id,
        "name": "Лента",
        "documents_kept": 3,
        "items_hidden": 0,
        "tracked_npa": 0,
    }
    assert db.sources.get(source.id).status == "deleted"
    assert db.documents.count(source.id) == 3
    assert db.sources.list() == []


def test_soft_delete_leaves_the_cards_visible_by_default(service, db, item_factory):
    source = _add(db)
    document = _document(db, source.id, "d0")
    item_id = item_factory(db, document)

    report = service.soft_delete(source.id)

    assert report["items_hidden"] == 0
    assert db.items.get(item_id).visibility == "visible"


def test_soft_delete_with_purge_items_hides_the_cards_without_deleting_them(service, db,
                                                                            item_factory):
    source = _add(db)
    document = _document(db, source.id, "d0")
    item_id = item_factory(db, document)

    report = service.soft_delete(source.id, purge_items=True)

    assert report["items_hidden"] == 1
    item = db.items.get(item_id)
    assert item.visibility == "hidden_feed"
    assert item.hidden_reason == "источник удалён"
    assert db.items.count() == 1


def test_soft_delete_counts_the_tracked_bills_it_stops_feeding(service, db, item_factory, caplog):
    source = _add(db)
    document = _document(db, source.id, "d0")
    item_factory(db, document, type="npa", npa_key="112233-8")

    report = service.soft_delete(source.id)

    assert report["tracked_npa"] == 1


def test_soft_delete_of_a_missing_source_is_a_named_error(service):
    with pytest.raises(SourceError) as info:
        service.soft_delete(42)
    assert info.value.code == "source_not_found"


def test_restore_brings_a_deleted_source_back_and_reschedules_it(service, db):
    source = _add(db)
    service.soft_delete(source.id)

    restored = service.restore(source.id)

    assert restored.status == "active"
    assert restored.deleted_at is None
    assert restored.next_run_at == "2026-09-02T12:00:00+00:00"
    stored = db.sources.get(source.id)
    assert (stored.status, stored.deleted_at) == ("active", None)
    assert [s.id for s in db.sources.due("2026-09-02T12:00:00+00:00")] == [source.id]


def test_restore_does_not_unhide_the_cards_purged_with_the_source(service, db, item_factory):
    source = _add(db)
    item_id = item_factory(db, _document(db, source.id, "d0"))
    service.soft_delete(source.id, purge_items=True)

    service.restore(source.id)

    assert db.items.get(item_id).visibility == "hidden_feed"


# ── health ─────────────────────────────────────────────────────────────────


def test_health_reports_the_source_its_documents_and_its_polls(service, db):
    source = _add(db)
    _document(db, source.id, "d0")
    with db.transaction():
        db.fetch_state.save(
            FetchState(
                source_id=source.id,
                last_success_at="2026-09-02T10:00:00+00:00",
                last_error="HTTP 503",
                consecutive_failures=2,
            )
        )
    run_id = db.source_runs.start(source.id, "2026-09-02T11:00:00+00:00")
    db.source_runs.finish(run_id, items_found=4, items_new=1)

    data = service.health(source.id)

    assert data["source"].id == source.id
    assert data["documents"] == 1
    assert data["consecutive_failures"] == 2
    assert data["last_success_at"] == "2026-09-02T10:00:00+00:00"
    assert data["last_error"] == "HTTP 503"
    assert [(r.items_found, r.items_new) for r in data["runs"]] == [(4, 1)]


def test_health_of_a_source_that_was_never_polled_is_empty_but_valid(service, db):
    source = _add(db)
    data = service.health(source.id)
    assert (data["documents"], data["consecutive_failures"]) == (0, 0)
    assert (data["last_success_at"], data["last_error"]) == (None, None)
    assert data["runs"] == []


def test_health_honours_the_limit(service, db):
    source = _add(db)
    for hour in range(3):
        db.source_runs.start(source.id, f"2026-09-02T1{hour}:00:00+00:00")
    assert len(service.health(source.id, limit=2)["runs"]) == 2


def test_health_of_a_missing_source_is_a_named_error(service):
    with pytest.raises(SourceError) as info:
        service.health(42)
    assert info.value.code == "source_not_found"


# ── the error object itself ────────────────────────────────────────────────


def test_source_error_carries_a_machine_code_and_details():
    """Код и статус живут на классе; текст и подробности — на экземпляре."""
    error = SourceExistsError("уже добавлен", {"source_id": 7})

    assert (error.code, error.message, error.details) == (
        "source_exists", "уже добавлен", {"source_id": 7}
    )
    assert error.detail == error.message
    assert error.status_code == 409
    assert str(error) == "уже добавлен"
    assert isinstance(error, SourceError)
    assert SourceExistsError("y").details == {}
