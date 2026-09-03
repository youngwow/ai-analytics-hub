"""src/sources/scraper_tg.py — t.me/s/<channel> parsing, cursor pagination, MTProto transport.

The MTProto half is driven by `FakeReader`, a stand-in for `MtprotoReader`, so no
test here imports telethon or opens a socket.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from selectolax.parser import HTMLParser
from support import EMPTY_CHANNEL_PAGE, HTML_UTF8, MockRoutes, raising

from src.config import Config
from src.models import FetchState, Source
from src.sources.base import HostLimiter
from src.sources.scraper_tg import (
    NO_CREDENTIALS,
    PREVIEW_UNAVAILABLE,
    TelegramAdapter,
    parse_channel_page,
    parse_message,
    parse_post_id,
)
from src.sources.telegram_mtproto import (
    FILE_PREFIX,
    MtChannel,
    MtPost,
    MtprotoError,
    MtprotoUnavailable,
)

BASE = "https://t.me/s/cit_gov"
OTHER_BASE = "https://t.me/s/mintsifry"
CHANNEL_TITLE = "Цифровые индустриальные технологии"
FOOTER_LINK = "https://max.ru/id7727009915_gos"  # every post ends with this promo link
POST_DATE = datetime(2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc)


def _source(**overrides) -> Source:
    base = dict(id=3, name="cit_gov", url="https://t.me/cit_gov", kind="telegram", fetch_url=BASE)
    return Source(**{**base, **overrides})


def _adapter(config) -> TelegramAdapter:
    return TelegramAdapter(config.scraper, config.telegram)


# ── MTProto seam ───────────────────────────────────────────────────────────


class FakeReader:
    """An `MtprotoReader` over canned channels that records every call it gets."""

    def __init__(self, channels: dict[str, MtChannel] | None = None, error: Exception | None = None):
        self.channels = channels or {}
        self.error = error  # raised by `channel_posts` until it is cleared
        self.calls: list[dict] = []
        self.closed = 0

    def channel_posts(self, channel, *, min_id=0, limit=100, offset_date=None) -> MtChannel:
        self.calls.append(
            {"channel": channel, "min_id": min_id, "limit": limit, "offset_date": offset_date}
        )
        if self.error is not None:
            raise self.error
        return self.channels.get(channel, MtChannel())

    def close(self) -> None:
        self.closed += 1


class CountingFactory:
    """A `mtproto_factory` handing out one reader, counting how often it was asked."""

    def __init__(self, reader: FakeReader):
        self.reader = reader
        self.calls = 0

    def __call__(self) -> FakeReader:
        self.calls += 1
        return self.reader


def _mt_config(raw_config, mode: str = "auto", **telegram) -> Config:
    raw_config["telegram"].update(mtproto=mode, **telegram)
    return Config.from_dict(raw_config)


def _mt_adapter(config, factory=None) -> TelegramAdapter:
    return TelegramAdapter(config.scraper, config.telegram, HostLimiter(2), factory)


def _channel(*posts: MtPost, title: str = CHANNEL_TITLE) -> dict[str, MtChannel]:
    return {"cit_gov": MtChannel(title=title, posts=list(posts))}


def _message(inner: str, data_post: str = "cit_gov/42", when: str = "2026-09-02T10:00:00+00:00"):
    html = (
        f'<div class="tgme_widget_message" data-post="{data_post}">{inner}'
        f'<a class="tgme_widget_message_date" href="https://t.me/{data_post}">'
        f'<time datetime="{when}"></time></a></div>'
    )
    return HTMLParser(html).css_first(".tgme_widget_message")


# ── parse_post_id ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("cit_gov/123", 123),
        ("Digital_Gov1/1", 1),
        ("cit_gov/", None),
        ("cit_gov", None),
        ("cit-gov/12", None),
        ("", None),
        (None, None),
        ("cit_gov/12/extra", None),
    ],
)
def test_parse_post_id(value, expected):
    assert parse_post_id(value) == expected


# ── parse_channel_page ─────────────────────────────────────────────────────


def test_parse_channel_page_reads_title_ids_and_documents(fixture_bytes, now):
    page = parse_channel_page(fixture_bytes("tg_channel.html"), 3, now)
    assert page.error is None
    assert page.title == CHANNEL_TITLE
    assert page.post_ids == [1482, 1483, 1484, 1485]
    assert [d.external_id for d in page.documents] == [
        "cit_gov/1482",
        "cit_gov/1483",
        "cit_gov/1484",
        "cit_gov/1485",
    ]
    first = page.documents[0]
    assert first.source_id == 3
    assert first.url == "https://t.me/cit_gov/1482"
    assert first.title == "🎲 CAM для подготовки производства"
    assert first.author == CHANNEL_TITLE
    assert first.published_at == "2026-08-18T10:31:52+00:00"
    assert first.fetched_at == "2026-09-02T12:00:00+00:00"
    assert first.text.startswith("🎲 CAM для подготовки производства\n\nCAM (Computer-Aided Manufacturing)")
    assert first.attachments == [FOOTER_LINK]
    assert first.needs_fulltext is False
    assert first.summary == ""


def test_parse_channel_page_external_links_become_attachments(fixture_bytes, now):
    page = parse_channel_page(fixture_bytes("tg_channel.html"), 3, now)
    by_id = {d.external_id: d for d in page.documents}
    assert by_id["cit_gov/1483"].attachments == ["https://vk.cc/d0AT2M", FOOTER_LINK]
    assert by_id["cit_gov/1484"].attachments == [
        "https://digitalattache.ru/turkmenistan",
        "https://rutube.ru/video/8349cb03dfa33f652fa1443e474d4261/",
        "https://tkm.minpromtorg.gov.ru/",
        "https://digitalattache.ru/form",
    ]
    for doc in page.documents:
        assert not any("t.me/" in a for a in doc.attachments)
    assert [d.published_at for d in page.documents] == [
        "2026-08-18T10:31:52+00:00",
        "2026-08-19T10:01:49+00:00",
        "2026-08-20T08:04:14+00:00",
        "2026-08-21T15:05:21+00:00",
    ]


def test_parse_channel_page_title_is_cut_at_word_boundary(fixture_bytes, now):
    page = parse_channel_page(fixture_bytes("tg_channel_before.html"), 3, now)
    long_post = next(d for d in page.documents if d.external_id == "cit_gov/1460")
    assert long_post.title == (
        "⚡️ Леван Дараселия: «цифровые атташе» выступают связующим звеном между "
        "российским бизнесом и партнерами в государстве…"
    )
    assert len(long_post.title) <= 121


def test_parse_channel_page_skips_media_only_posts_but_keeps_their_ids(fixture_bytes, now):
    page = parse_channel_page(fixture_bytes("tg_channel_after.html"), 3, now)
    assert page.post_ids == [1499, 1500, 1501]
    assert [d.external_id for d in page.documents] == ["cit_gov/1501"]
    assert page.documents[0].title == "*️⃣ Kazan Digital Week — 2026: уже скоро!"
    assert page.documents[0].published_at == "2026-09-02T14:50:01+00:00"


def test_parse_channel_page_preview_unavailable(fixture_bytes, now):
    page = parse_channel_page(fixture_bytes("tg_preview_unavailable.html"), 3, now)
    assert page.error == PREVIEW_UNAVAILABLE
    assert page.documents == []
    assert page.post_ids is None


def test_parse_channel_page_empty_channel_is_not_an_error(now):
    page = parse_channel_page(EMPTY_CHANNEL_PAGE, 3, now)
    assert page.error is None
    assert page.documents == []
    assert page.post_ids == []
    assert page.title == CHANNEL_TITLE


def test_parse_channel_page_falls_back_to_owner_name_for_title(now):
    body = (
        '<div class="tgme_channel_info"></div>'
        '<div class="tgme_widget_message" data-post="chan/5">'
        '<a class="tgme_widget_message_owner_name"><span>Имя канала</span></a>'
        '<div class="tgme_widget_message_text">Текст</div></div>'
    ).encode()
    page = parse_channel_page(body, 3, now)
    assert page.title == "Имя канала"
    assert page.documents[0].author == "Имя канала"


# ── parse_message ──────────────────────────────────────────────────────────


def test_parse_message_media_only_post_is_none(now):
    node = _message('<div class="tgme_widget_message_photo_wrap"></div>')
    assert parse_message(node, 3, CHANNEL_TITLE, now) is None


def test_parse_message_without_data_post_is_none(now):
    node = HTMLParser('<div class="tgme_widget_message">x</div>').css_first("div")
    assert parse_message(node, 3, CHANNEL_TITLE, now) is None


def test_parse_message_document_only_post_uses_document_title(now):
    node = _message(
        '<a class="tgme_widget_message_document_wrap" href="https://t.me/cit_gov/42?single">'
        '<div class="tgme_widget_message_document_title">Дайджест.pdf</div></a>'
    )
    doc = parse_message(node, 3, CHANNEL_TITLE, now)
    assert doc.title == "Дайджест.pdf"
    assert doc.text == ""
    assert doc.attachments == ["https://t.me/cit_gov/42?single"]
    assert doc.external_id == "cit_gov/42"
    assert doc.url == "https://t.me/cit_gov/42"
    assert doc.published_at == "2026-09-02T10:00:00+00:00"


def test_parse_message_document_without_title_gets_channel_placeholder(now):
    node = _message('<a class="tgme_widget_message_document_wrap" href="https://cdn/x.pdf"></a>')
    doc = parse_message(node, 3, CHANNEL_TITLE, now)
    assert doc.title == f"Документ из {CHANNEL_TITLE}"
    node = _message('<a class="tgme_widget_message_document_wrap" href="https://cdn/x.pdf"></a>')
    assert parse_message(node, 3, "", now).title == "Документ из cit_gov"


def test_parse_message_dedupes_attachments_and_drops_tme_links(now):
    node = _message(
        '<a class="tgme_widget_message_document_wrap" href="https://cdn/x.pdf"></a>'
        '<div class="tgme_widget_message_text">Заголовок<br><br>'
        '<a href="https://cdn/x.pdf">файл</a> <a href="https://t.me/other/1">tg</a> '
        '<a href="https://site.ru/a">a</a> <a href="https://site.ru/a">a again</a> '
        '<a href="/relative">rel</a></div>'
    )
    doc = parse_message(node, 3, CHANNEL_TITLE, now)
    assert doc.attachments == ["https://cdn/x.pdf", "https://site.ru/a"]
    assert doc.title == "Заголовок"
    assert doc.text == "Заголовок\n\nфайл tg a a again rel"


def test_parse_message_without_time_has_no_date(now):
    html = (
        '<div class="tgme_widget_message" data-post="cit_gov/1">'
        '<div class="tgme_widget_message_text">Текст</div></div>'
    )
    node = HTMLParser(html).css_first(".tgme_widget_message")
    assert parse_message(node, 3, CHANNEL_TITLE, now).published_at is None


# ── TelegramAdapter.fetch ──────────────────────────────────────────────────


def test_first_run_reads_base_page_once_and_sets_cursor(config, mock_client, fixture_bytes, now):
    routes = MockRoutes({BASE: (200, fixture_bytes("tg_channel.html"), HTML_UTF8)})
    result = _adapter(config).fetch(_source(), FetchState(source_id=3), mock_client(routes), now=now)
    assert result.error is None
    assert routes.urls() == [BASE]
    assert [d.external_id for d in result.documents] == [
        "cit_gov/1482",
        "cit_gov/1483",
        "cit_gov/1484",
        "cit_gov/1485",
    ]
    assert result.state_update == {"cursor": {"last_post_id": 1485, "transport": "web"}}
    assert result.source_title == CHANNEL_TITLE


def test_incremental_run_pages_after_cursor_until_nothing_newer(config, mock_client, fixture_bytes, now):
    routes = MockRoutes(
        {
            f"{BASE}?after=1498": (200, fixture_bytes("tg_channel_after.html"), HTML_UTF8),
            f"{BASE}?after=1501": (200, EMPTY_CHANNEL_PAGE, HTML_UTF8),
        }
    )
    state = FetchState(
        source_id=3,
        last_success_at="2026-09-01T00:00:00+00:00",
        cursor={"last_post_id": 1498, "extra": "kept"},
    )
    result = _adapter(config).fetch(_source(), state, mock_client(routes), now=now)
    assert result.error is None
    assert routes.urls() == [f"{BASE}?after=1498", f"{BASE}?after=1501"]
    assert [d.external_id for d in result.documents] == ["cit_gov/1501"]
    assert result.state_update == {
        "cursor": {"last_post_id": 1501, "extra": "kept", "transport": "web"}
    }
    assert result.source_title == CHANNEL_TITLE


def test_incremental_run_drops_posts_at_or_below_cursor(config, mock_client, fixture_bytes, now):
    # The ?after= page echoes ids up to the cursor; only strictly newer ones count.
    routes = MockRoutes(
        {
            f"{BASE}?after=1500": (200, fixture_bytes("tg_channel_after.html"), HTML_UTF8),
            f"{BASE}?after=1501": (200, EMPTY_CHANNEL_PAGE, HTML_UTF8),
        }
    )
    state = FetchState(source_id=3, last_success_at="x", cursor={"last_post_id": 1500})
    result = _adapter(config).fetch(_source(), state, mock_client(routes), now=now)
    assert [d.external_id for d in result.documents] == ["cit_gov/1501"]
    assert result.state_update["cursor"]["last_post_id"] == 1501


def test_incremental_run_with_nothing_new_keeps_cursor(config, mock_client, now):
    routes = MockRoutes({f"{BASE}?after=1498": (200, EMPTY_CHANNEL_PAGE, HTML_UTF8)})
    state = FetchState(source_id=3, last_success_at="x", cursor={"last_post_id": 1498})
    result = _adapter(config).fetch(_source(), state, mock_client(routes), now=now)
    assert result.error is None
    assert result.documents == []
    assert routes.urls() == [f"{BASE}?after=1498"]
    assert result.state_update == {"cursor": {"last_post_id": 1498, "transport": "web"}}


def _synthetic_page(ids: list[int]) -> bytes:
    posts = "".join(
        f'<div class="tgme_widget_message" data-post="cit_gov/{i}">'
        f'<div class="tgme_widget_message_text">Пост номер {i}</div></div>'
        for i in ids
    )
    return f'<html><body><div class="tgme_channel_info"></div>{posts}</body></html>'.encode()


def test_incremental_run_stops_after_max_pages(config, mock_client, now):
    from src.sources import scraper_tg

    def endless(request):  # every ?after=N page answers with N+1..N+3
        after = int(request.url.params["after"])
        return (200, _synthetic_page([after + 1, after + 2, after + 3]), HTML_UTF8)

    routes = MockRoutes(default=endless)
    state = FetchState(source_id=3, last_success_at="x", cursor={"last_post_id": 1000})
    result = _adapter(config).fetch(_source(), state, mock_client(routes), now=now)
    assert result.error is None
    assert routes.urls() == [
        f"{BASE}?after={1000 + 3 * i}" for i in range(scraper_tg.MAX_INCREMENTAL_PAGES)
    ]
    assert [d.external_id for d in result.documents] == [
        f"cit_gov/{i}" for i in range(1001, 1001 + 3 * scraper_tg.MAX_INCREMENTAL_PAGES)
    ]
    assert result.state_update == {
        "cursor": {
            "last_post_id": 1000 + 3 * scraper_tg.MAX_INCREMENTAL_PAGES,
            "transport": "web",
        }
    }


def test_backfill_walks_before_pages(config, mock_client, fixture_bytes, now):
    routes = MockRoutes(
        {
            BASE: (200, fixture_bytes("tg_channel.html"), HTML_UTF8),
            f"{BASE}?before=1482": (200, fixture_bytes("tg_channel_before.html"), HTML_UTF8),
        }
    )
    state = FetchState(source_id=3, last_success_at="x", cursor={"last_post_id": 1485})
    result = _adapter(config).fetch(
        _source(), state, mock_client(routes), now=now, backfill=True
    )
    assert result.error is None
    assert routes.urls() == [BASE, f"{BASE}?before=1482"]
    assert [d.external_id for d in result.documents] == [
        "cit_gov/1482",
        "cit_gov/1483",
        "cit_gov/1484",
        "cit_gov/1485",
        "cit_gov/1459",
        "cit_gov/1460",
        "cit_gov/1461",
    ]
    assert result.state_update == {"cursor": {"last_post_id": 1485, "transport": "web"}}


def test_backfill_respects_backfill_pages(raw_config, mock_client, fixture_bytes, now):
    from src.config import Config

    raw_config["telegram"]["backfill_pages"] = 2
    config = Config.from_dict(raw_config)
    routes = MockRoutes(
        {
            BASE: (200, fixture_bytes("tg_channel.html"), HTML_UTF8),
            f"{BASE}?before=1482": (200, fixture_bytes("tg_channel_before.html"), HTML_UTF8),
            f"{BASE}?before=1459": (200, EMPTY_CHANNEL_PAGE, HTML_UTF8),
        }
    )
    result = _adapter(config).fetch(
        _source(), FetchState(source_id=3), mock_client(routes), now=now, backfill=True
    )
    assert routes.urls() == [BASE, f"{BASE}?before=1482", f"{BASE}?before=1459"]
    assert len(result.documents) == 7


def test_backfill_stops_when_an_older_page_fails(config, mock_client, fixture_bytes, now):
    routes = MockRoutes(
        {
            BASE: (200, fixture_bytes("tg_channel.html"), HTML_UTF8),
            f"{BASE}?before=1482": (503, b"", {}),
        }
    )
    result = _adapter(config).fetch(
        _source(), FetchState(source_id=3), mock_client(routes), now=now, backfill=True
    )
    assert result.error is None
    assert len(result.documents) == 4


def test_redirect_away_from_preview_is_reported(config, mock_client, fixture_bytes, now):
    secret = "https://t.me/s/secretchan"
    routes = MockRoutes(
        {
            secret: (302, b"", {"location": "https://t.me/secretchan"}),
            "https://t.me/secretchan": (200, fixture_bytes("tg_preview_unavailable.html"), HTML_UTF8),
        }
    )
    result = _adapter(config).fetch(
        _source(fetch_url=secret), FetchState(source_id=3), mock_client(routes), now=now
    )
    assert result.error == PREVIEW_UNAVAILABLE
    assert result.documents == []


def test_preview_unavailable_page_is_reported(config, mock_client, fixture_bytes, now):
    routes = MockRoutes({BASE: (200, fixture_bytes("tg_preview_unavailable.html"), HTML_UTF8)})
    result = _adapter(config).fetch(_source(), FetchState(source_id=3), mock_client(routes), now=now)
    assert result.error == PREVIEW_UNAVAILABLE


@pytest.mark.parametrize(
    ("reply", "expected_error"),
    [
        ((404, b"", {}), "HTTP 404"),
        ((500, b"", {}), "HTTP 500"),
        (raising(httpx.ReadTimeout("slow")), "timeout"),
    ],
    ids=["404", "500", "timeout"],
)
def test_http_and_transport_errors(config, mock_client, now, reply, expected_error):
    routes = MockRoutes({BASE: reply})
    result = _adapter(config).fetch(_source(), FetchState(source_id=3), mock_client(routes), now=now)
    assert result.error == expected_error
    assert result.state_update == {}


def test_incremental_page_error_aborts_the_run(config, mock_client, now):
    routes = MockRoutes({f"{BASE}?after=1498": (500, b"", {})})
    state = FetchState(source_id=3, last_success_at="x", cursor={"last_post_id": 1498})
    result = _adapter(config).fetch(_source(), state, mock_client(routes), now=now)
    assert result.error == "HTTP 500"
    assert result.documents == []


def test_trailing_slash_in_fetch_url_is_tolerated(config, mock_client, fixture_bytes, now):
    routes = MockRoutes({BASE: (200, fixture_bytes("tg_channel.html"), HTML_UTF8)})
    result = _adapter(config).fetch(
        _source(fetch_url=BASE + "/"), FetchState(source_id=3), mock_client(routes), now=now
    )
    assert result.error is None
    assert routes.urls() == [BASE]


# ── TelegramAdapter.fetch over MTProto ─────────────────────────────────────


class TestMtprotoTransport:
    def test_auto_mode_reads_over_mtproto_and_makes_no_http(self, raw_config, mock_client, now):
        config = _mt_config(raw_config, "auto")
        reader = FakeReader(
            _channel(
                MtPost(id=1490, text="Пост А\n\nПодробности", date=POST_DATE),
                MtPost(id=1491, text="Пост Б", date=POST_DATE, urls=["https://gov.ru/doc"],
                       files=["Приказ.pdf"]),
            )
        )
        routes = MockRoutes()
        result = _mt_adapter(config, lambda: reader).fetch(
            _source(), FetchState(source_id=3), mock_client(routes), now=now
        )

        assert result.error is None
        assert routes.requests == []
        assert [d.external_id for d in result.documents] == ["cit_gov/1490", "cit_gov/1491"]
        assert result.state_update == {"cursor": {"last_post_id": 1491, "transport": "mtproto"}}
        assert result.source_title == CHANNEL_TITLE
        first, second = result.documents
        assert first.url == "https://t.me/cit_gov/1490"
        assert first.title == "Пост А"
        assert first.source_id == 3
        assert first.author == CHANNEL_TITLE
        assert first.published_at == "2026-09-02T10:00:00+00:00"
        assert first.fetched_at == "2026-09-02T12:00:00+00:00"
        assert second.attachments == [f"{FILE_PREFIX}Приказ.pdf", "https://gov.ru/doc"]

    def test_cursor_counts_posts_that_produced_no_document(self, raw_config, mock_client, now):
        # 1501 is a photo: no document, but re-reading it next run would be wasted work.
        config = _mt_config(raw_config, "auto")
        reader = FakeReader(
            _channel(MtPost(id=1500, text="Пост", date=POST_DATE), MtPost(id=1501, date=POST_DATE))
        )
        result = _mt_adapter(config, lambda: reader).fetch(
            _source(), FetchState(source_id=3), mock_client(MockRoutes()), now=now
        )
        assert [d.external_id for d in result.documents] == ["cit_gov/1500"]
        assert result.state_update["cursor"]["last_post_id"] == 1501

    def test_empty_channel_records_the_transport_and_keeps_the_cursor(
        self, raw_config, mock_client, now
    ):
        config = _mt_config(raw_config, "auto")
        reader = FakeReader(_channel())
        state = FetchState(source_id=3, last_success_at="x", cursor={"last_post_id": 1485})
        result = _mt_adapter(config, lambda: reader).fetch(
            _source(), state, mock_client(MockRoutes()), now=now
        )
        assert result.error is None
        assert result.documents == []
        assert result.state_update == {"cursor": {"last_post_id": 1485, "transport": "mtproto"}}

    def test_first_run_bounds_the_history_by_date(self, raw_config, mock_client, now):
        config = _mt_config(raw_config, "auto")
        reader = FakeReader(_channel(MtPost(id=1, text="Пост", date=POST_DATE)))
        since = now - timedelta(hours=72)
        _mt_adapter(config, lambda: reader).fetch(
            _source(), FetchState(source_id=3), mock_client(MockRoutes()), now=now, since=since
        )
        assert reader.calls[0]["channel"] == "cit_gov"
        assert reader.calls[0]["min_id"] == 0
        assert reader.calls[0]["offset_date"] == since

    @pytest.mark.parametrize(
        "cursor",
        [
            {"last_post_id": 1485, "transport": "web"},
            {"last_post_id": 1485, "transport": "mtproto"},
            {"last_post_id": 1485},
        ],
        ids=["written-by-the-web-path", "written-by-mtproto", "written-before-transport-existed"],
    )
    def test_an_existing_cursor_bounds_the_history_by_post_id(
        self, raw_config, mock_client, now, cursor
    ):
        config = _mt_config(raw_config, "auto")
        reader = FakeReader(_channel(MtPost(id=1486, text="Пост", date=POST_DATE)))
        state = FetchState(source_id=3, last_success_at="x", cursor={**cursor, "extra": "kept"})
        result = _mt_adapter(config, lambda: reader).fetch(
            _source(), state, mock_client(MockRoutes()), now=now, since=now - timedelta(hours=72)
        )
        assert reader.calls[0]["min_id"] == 1485
        assert reader.calls[0]["offset_date"] is None
        assert result.state_update == {
            "cursor": {"last_post_id": 1486, "transport": "mtproto", "extra": "kept"}
        }

    @pytest.mark.parametrize(
        ("max_posts", "max_new_per_source", "expected"),
        [(40, 25, 25), (10, 50, 10)],
        ids=["collector-cap-is-smaller", "telegram-cap-is-smaller"],
    )
    def test_limit_is_the_smaller_of_the_two_caps(
        self, raw_config, mock_client, now, max_posts, max_new_per_source, expected
    ):
        raw_config["scraper"]["max_new_per_source"] = max_new_per_source
        config = _mt_config(raw_config, "auto", max_posts=max_posts, backfill_posts=300)
        reader = FakeReader(_channel())
        _mt_adapter(config, lambda: reader).fetch(
            _source(), FetchState(source_id=3), mock_client(MockRoutes()), now=now
        )
        assert reader.calls[0]["limit"] == expected

    def test_backfill_asks_for_backfill_posts_over_the_whole_history(
        self, raw_config, mock_client, now
    ):
        config = _mt_config(raw_config, "auto", max_posts=40, backfill_posts=250)
        reader = FakeReader(_channel(MtPost(id=7, text="Старый пост", date=POST_DATE)))
        state = FetchState(source_id=3, last_success_at="x", cursor={"last_post_id": 1485})
        result = _mt_adapter(config, lambda: reader).fetch(
            _source(), state, mock_client(MockRoutes()), now=now,
            since=now - timedelta(hours=72), backfill=True,
        )
        assert reader.calls == [
            {"channel": "cit_gov", "min_id": 0, "limit": 250, "offset_date": None}
        ]
        assert [d.external_id for d in result.documents] == ["cit_gov/7"]
        assert result.state_update["cursor"]["last_post_id"] == 1485  # the cursor never goes back

    def test_off_mode_ignores_the_factory_and_reads_the_web_page(
        self, raw_config, mock_client, fixture_bytes, now
    ):
        config = _mt_config(raw_config, "off")
        factory = CountingFactory(FakeReader(_channel(MtPost(id=9999, text="Не должно попасть"))))
        routes = MockRoutes({BASE: (200, fixture_bytes("tg_channel.html"), HTML_UTF8)})
        result = _mt_adapter(config, factory).fetch(
            _source(), FetchState(source_id=3), mock_client(routes), now=now
        )
        assert factory.calls == 0
        assert factory.reader.calls == []
        assert routes.urls() == [BASE]
        assert [d.external_id for d in result.documents][0] == "cit_gov/1482"
        assert result.state_update["cursor"]["transport"] == "web"

    def test_only_mode_without_a_factory_errors_without_touching_anything(
        self, raw_config, mock_client, now
    ):
        config = _mt_config(raw_config, "only")
        routes = MockRoutes()
        result = _mt_adapter(config, None).fetch(
            _source(), FetchState(source_id=3), mock_client(routes), now=now
        )
        assert result.error == NO_CREDENTIALS
        assert "telegram login" in result.error
        assert result.documents == []
        assert result.state_update == {}
        assert routes.requests == []

    def test_a_per_channel_error_in_auto_mode_falls_back_to_the_web_page(
        self, raw_config, mock_client, fixture_bytes, now, caplog
    ):
        config = _mt_config(raw_config, "auto")
        reader = FakeReader(error=MtprotoError("channel is private or the account was banned"))
        routes = MockRoutes({BASE: (200, fixture_bytes("tg_channel.html"), HTML_UTF8)})
        with caplog.at_level("WARNING", logger="telegram"):
            result = _mt_adapter(config, lambda: reader).fetch(
                _source(), FetchState(source_id=3), mock_client(routes), now=now
            )
        assert result.error is None
        assert len(reader.calls) == 1
        assert routes.urls() == [BASE]
        assert [d.external_id for d in result.documents] == [
            "cit_gov/1482", "cit_gov/1483", "cit_gov/1484", "cit_gov/1485",
        ]
        assert result.state_update == {"cursor": {"last_post_id": 1485, "transport": "web"}}
        assert "falling back to the preview" in caplog.text

    def test_a_per_channel_error_in_only_mode_is_reported_with_a_prefix(
        self, raw_config, mock_client, fixture_bytes, now
    ):
        config = _mt_config(raw_config, "only")
        reader = FakeReader(error=MtprotoError("flood wait 300s"))
        routes = MockRoutes({BASE: (200, fixture_bytes("tg_channel.html"), HTML_UTF8)})
        result = _mt_adapter(config, lambda: reader).fetch(
            _source(), FetchState(source_id=3), mock_client(routes), now=now
        )
        assert result.error == "MTProto: flood wait 300s"
        assert result.documents == []
        assert result.state_update == {}
        assert routes.requests == []

    def test_a_per_channel_error_does_not_latch_mtproto_off(
        self, raw_config, mock_client, fixture_bytes, now
    ):
        config = _mt_config(raw_config, "auto")
        reader = FakeReader(error=MtprotoError("no such channel"))
        factory = CountingFactory(reader)
        routes = MockRoutes(
            {
                BASE: (200, fixture_bytes("tg_channel.html"), HTML_UTF8),
                OTHER_BASE: (200, fixture_bytes("tg_channel.html"), HTML_UTF8),
            }
        )
        adapter = _mt_adapter(config, factory)
        client = mock_client(routes)
        adapter.fetch(_source(), FetchState(source_id=3), client, now=now)

        reader.error = None  # only the first channel was unreadable
        reader.channels = {"mintsifry": MtChannel(title="Минцифры",
                                                  posts=[MtPost(id=90, text="Пост", date=POST_DATE)])}
        result = adapter.fetch(
            _source(id=4, fetch_url=OTHER_BASE), FetchState(source_id=4), client, now=now
        )
        assert len(reader.calls) == 2  # MTProto is still tried for the next source
        assert factory.calls == 1  # …over the same connection
        assert [d.external_id for d in result.documents] == ["mintsifry/90"]
        assert routes.urls() == [BASE]  # only the failed channel fell back to the web

    def test_an_unavailable_session_in_auto_mode_falls_back_and_latches(
        self, raw_config, mock_client, fixture_bytes, now, caplog
    ):
        config = _mt_config(raw_config, "auto")
        reader = FakeReader(error=MtprotoUnavailable("Telegram session is no longer valid"))
        factory = CountingFactory(reader)
        routes = MockRoutes(
            {
                BASE: (200, fixture_bytes("tg_channel.html"), HTML_UTF8),
                OTHER_BASE: (200, fixture_bytes("tg_channel.html"), HTML_UTF8),
            }
        )
        adapter = _mt_adapter(config, factory)
        client = mock_client(routes)
        with caplog.at_level("WARNING", logger="telegram"):
            first = adapter.fetch(_source(), FetchState(source_id=3), client, now=now)
        second = adapter.fetch(
            _source(id=4, fetch_url=OTHER_BASE), FetchState(source_id=4), client, now=now
        )

        assert first.error is None and second.error is None
        assert len(first.documents) == 4 and len(second.documents) == 4
        assert factory.calls == 1  # the dead session is not reconnected per source
        assert len(reader.calls) == 1  # …and not re-asked either
        assert routes.urls() == [BASE, OTHER_BASE]
        assert "MTProto unavailable" in caplog.text

    def test_an_unavailable_session_in_only_mode_is_reported_with_a_prefix(
        self, raw_config, mock_client, now
    ):
        config = _mt_config(raw_config, "only")
        reader = FakeReader(error=MtprotoUnavailable("no Telegram session: run `login`"))
        routes = MockRoutes()
        result = _mt_adapter(config, lambda: reader).fetch(
            _source(), FetchState(source_id=3), mock_client(routes), now=now
        )
        assert result.error == "MTProto: no Telegram session: run `login`"
        assert routes.requests == []

    def test_close_disconnects_the_reader_once(self, raw_config, mock_client, now):
        config = _mt_config(raw_config, "auto")
        reader = FakeReader(_channel(MtPost(id=1, text="Пост", date=POST_DATE)))
        adapter = _mt_adapter(config, lambda: reader)
        adapter.fetch(_source(), FetchState(source_id=3), mock_client(MockRoutes()), now=now)
        adapter.close()
        adapter.close()
        assert reader.closed == 1

    def test_close_without_a_reader_is_a_no_op(self, raw_config):
        config = _mt_config(raw_config, "auto")
        reader = FakeReader()
        _mt_adapter(config, lambda: reader).close()
        assert reader.closed == 0

    def test_close_clears_the_unavailable_latch(self, raw_config, mock_client, fixture_bytes, now):
        config = _mt_config(raw_config, "auto")
        reader = FakeReader(error=MtprotoUnavailable("session revoked"))
        factory = CountingFactory(reader)
        routes = MockRoutes({BASE: (200, fixture_bytes("tg_channel.html"), HTML_UTF8)})
        adapter = _mt_adapter(config, factory)
        adapter.fetch(_source(), FetchState(source_id=3), mock_client(routes), now=now)
        adapter.close()

        reader.error = None  # the next run signs in again
        reader.channels = _channel(MtPost(id=1490, text="Снова MTProto", date=POST_DATE))
        result = adapter.fetch(
            _source(), FetchState(source_id=3), mock_client(routes), now=now
        )
        assert factory.calls == 2
        assert reader.closed == 1
        assert [d.external_id for d in result.documents] == ["cit_gov/1490"]
        assert result.state_update["cursor"]["transport"] == "mtproto"

    @pytest.mark.parametrize(
        ("title", "expected"),
        [("Минцифры России", "Минцифры России"), ("", None)],
        ids=["channel-has-a-title", "channel-has-none"],
    )
    def test_source_title_is_the_channel_title(
        self, raw_config, mock_client, now, title, expected
    ):
        config = _mt_config(raw_config, "auto")
        reader = FakeReader(_channel(MtPost(id=1, text="Пост", date=POST_DATE), title=title))
        result = _mt_adapter(config, lambda: reader).fetch(
            _source(), FetchState(source_id=3), mock_client(MockRoutes()), now=now
        )
        assert result.source_title == expected
        assert result.documents[0].author == title

    @pytest.mark.parametrize(
        "fetch_url", ["https://t.me/", "https://t.me/s/", "https://t.me/s/ab"],
        ids=["no-channel", "no-channel-after-s", "too-short"],
    )
    def test_a_fetch_url_without_a_channel_name_errors_without_reading(
        self, raw_config, mock_client, now, fetch_url
    ):
        config = _mt_config(raw_config, "auto")
        reader = FakeReader()
        routes = MockRoutes()
        result = _mt_adapter(config, lambda: reader).fetch(
            _source(fetch_url=fetch_url), FetchState(source_id=3), mock_client(routes), now=now
        )
        assert result.error == f"cannot read a channel name from {fetch_url}"
        assert reader.calls == []
        assert routes.requests == []
        assert result.state_update == {}
