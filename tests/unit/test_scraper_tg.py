"""src/sources/scraper_tg.py — t.me/s/<channel> parsing and cursor pagination."""

from __future__ import annotations

import httpx
import pytest
from selectolax.parser import HTMLParser
from support import EMPTY_CHANNEL_PAGE, HTML_UTF8, MockRoutes, raising

from src.models import FetchState, Source
from src.sources.scraper_tg import (
    PREVIEW_UNAVAILABLE,
    TelegramAdapter,
    parse_channel_page,
    parse_message,
    parse_post_id,
)

BASE = "https://t.me/s/cit_gov"
CHANNEL_TITLE = "Цифровые индустриальные технологии"
FOOTER_LINK = "https://max.ru/id7727009915_gos"  # every post ends with this promo link


def _source(**overrides) -> Source:
    base = dict(id=3, name="cit_gov", url="https://t.me/cit_gov", kind="telegram", fetch_url=BASE)
    return Source(**{**base, **overrides})


def _adapter(config) -> TelegramAdapter:
    return TelegramAdapter(config.scraper, config.telegram)


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
    assert result.state_update == {"cursor": {"last_post_id": 1485}}
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
    assert result.state_update == {"cursor": {"last_post_id": 1501, "extra": "kept"}}
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
    assert result.state_update == {"cursor": {"last_post_id": 1498}}


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
    assert result.state_update == {"cursor": {"last_post_id": 1000 + 3 * scraper_tg.MAX_INCREMENTAL_PAGES}}


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
    assert result.state_update == {"cursor": {"last_post_id": 1485}}


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
