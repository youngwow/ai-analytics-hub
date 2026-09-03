"""src/sources/telegram_mtproto.py — the pure parts: posts → documents, env, session files.

Nothing here builds a client and nothing imports telethon: `posts_from_messages`
only ever reads a handful of attributes off a message, so the fakes below are
plain objects with exactly those attributes.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from selectolax.parser import HTMLParser

from src.paths import ProjectPaths
from src.sources.scraper_tg import parse_message
from src.sources.telegram_mtproto import (
    FILE_PREFIX,
    MtPost,
    TelegramCredentials,
    credentials_from_env,
    delete_session,
    post_to_document,
    posts_from_messages,
)

CHANNEL = "cit_gov"
CHANNEL_TITLE = "Цифровые индустриальные технологии"
MOSCOW_OFFSET = timezone(timedelta(hours=3))


# ── fakes for the Telethon message shape ───────────────────────────────────


class FakeFile:
    """`message.file`: `name` is None for photos, video notes and voice messages."""

    def __init__(self, name: str | None):
        self.name = name


class TextUrlEntity:
    """`MessageEntityTextUrl`: the href lives on the entity, the text is the caption."""

    def __init__(self, url: str):
        self.url = url


class UrlEntity:
    """`MessageEntityUrl`: no `.url` attribute; the matched text is the link itself."""


class FakePreview:
    """`message.web_preview`: the link card Telegram unfurled under the post."""

    def __init__(self, url: str):
        self.url = url


class FakeMessage:
    """Only the attributes `posts_from_messages` actually reads."""

    def __init__(
        self,
        id: int,
        *,
        text: str = "",
        date: datetime | None = None,
        action=None,
        grouped_id: int | None = None,
        file: FakeFile | None = None,
        entities: list[tuple[object, str]] | None = None,
        preview_url: str = "",
        entities_error: Exception | None = None,
    ):
        self.id = id
        self.raw_text = text
        self.date = date
        self.action = action
        self.grouped_id = grouped_id
        self.file = file
        self.web_preview = FakePreview(preview_url) if preview_url else None
        self._entities = entities or []
        self._entities_error = entities_error

    def get_entities_text(self):
        if self._entities_error is not None:
            raise self._entities_error
        return list(self._entities)


def test_nothing_under_test_pulls_telethon_into_the_process():
    """Every telethon import is lazy; a test that triggers one costs seconds of import time."""
    assert "telethon" not in sys.modules


def _web_node(post_id: int, text: str = "Заголовок"):
    """The `t.me/s/` markup for the same post, to compare the two transports."""
    html = (
        f'<div class="tgme_widget_message" data-post="{CHANNEL}/{post_id}">'
        f'<div class="tgme_widget_message_text">{text}</div></div>'
    )
    return HTMLParser(html).css_first(".tgme_widget_message")


# ── post_to_document ───────────────────────────────────────────────────────


class TestPostToDocument:
    def test_post_with_neither_text_nor_files_is_none(self, now):
        post = MtPost(id=42, date=now, urls=["https://gov.ru/x"])
        assert post_to_document(post, CHANNEL, 3, CHANNEL_TITLE, now) is None

    def test_external_id_and_url_match_the_web_transport(self, now):
        # Cursor and external_id continuity: switching transports must not re-import.
        post = MtPost(id=1482, text="Заголовок", date=now)
        doc = post_to_document(post, CHANNEL, 3, CHANNEL_TITLE, now)
        web = parse_message(_web_node(1482), 3, CHANNEL_TITLE, now)
        assert doc.external_id == web.external_id == "cit_gov/1482"
        assert doc.url == web.url == "https://t.me/cit_gov/1482"
        assert doc.title == web.title == "Заголовок"
        assert doc.author == web.author == CHANNEL_TITLE
        assert doc.source_id == web.source_id == 3

    def test_title_is_the_first_line_of_the_text(self, now):
        post = MtPost(id=7, text="🎲 CAM для подготовки производства\n\nCAM — это…", date=now)
        doc = post_to_document(post, CHANNEL, 3, CHANNEL_TITLE, now)
        assert doc.title == "🎲 CAM для подготовки производства"
        assert doc.text == "🎲 CAM для подготовки производства\n\nCAM — это…"

    def test_long_first_line_is_cut_at_a_word_boundary(self, now):
        post = MtPost(id=7, text="слово " * 40, date=now)
        doc = post_to_document(post, CHANNEL, 3, CHANNEL_TITLE, now)
        assert doc.title.endswith("…")
        assert len(doc.title) <= 121

    def test_file_only_post_takes_the_file_name_as_its_title(self, now):
        post = MtPost(id=8, files=["Дайджест.pdf"], date=now)
        doc = post_to_document(post, CHANNEL, 3, CHANNEL_TITLE, now)
        assert doc.title == "Дайджест.pdf"
        assert doc.text == ""
        assert doc.attachments == [f"{FILE_PREFIX}Дайджест.pdf"]

    @pytest.mark.parametrize(
        ("channel_title", "expected"),
        [(CHANNEL_TITLE, f"Документ из {CHANNEL_TITLE}"), ("", "Документ из cit_gov")],
        ids=["known-channel-title", "unknown-channel-title"],
    )
    def test_unnamed_file_falls_back_to_a_placeholder_title(self, now, channel_title, expected):
        post = MtPost(id=9, files=[""], date=now)
        doc = post_to_document(post, CHANNEL, 3, channel_title, now)
        assert doc.title == expected
        assert doc.attachments == []  # a nameless file gives nothing to link to

    def test_text_wins_over_the_file_name_as_the_title(self, now):
        post = MtPost(id=10, text="Опубликован приказ", files=["prikaz.pdf"], date=now)
        doc = post_to_document(post, CHANNEL, 3, CHANNEL_TITLE, now)
        assert doc.title == "Опубликован приказ"

    def test_attachments_list_files_first_then_urls_deduped(self, now):
        post = MtPost(
            id=11,
            text="Текст",
            files=["a.pdf", "b.pdf", "a.pdf", ""],
            urls=["https://gov.ru/1", "https://gov.ru/1", "https://cbr.ru/2"],
            date=now,
        )
        doc = post_to_document(post, CHANNEL, 3, CHANNEL_TITLE, now)
        assert doc.attachments == [
            f"{FILE_PREFIX}a.pdf",
            f"{FILE_PREFIX}b.pdf",
            "https://gov.ru/1",
            "https://cbr.ru/2",
        ]

    @pytest.mark.parametrize(
        ("date", "expected"),
        [
            (datetime(2026, 8, 18, 10, 31, 52, tzinfo=timezone.utc), "2026-08-18T10:31:52+00:00"),
            (datetime(2026, 8, 18, 13, 31, 52, tzinfo=MOSCOW_OFFSET), "2026-08-18T10:31:52+00:00"),
            (datetime(2026, 8, 18, 13, 31, 52), "2026-08-18T10:31:52+00:00"),
            (datetime(2026, 8, 18, 10, 31, 52, 987654, tzinfo=timezone.utc),
             "2026-08-18T10:31:52+00:00"),
            (None, None),
        ],
        ids=["utc", "moscow-offset", "naive-is-moscow", "microseconds-dropped", "no-date"],
    )
    def test_published_at_is_the_canonical_utc_string(self, now, date, expected):
        post = MtPost(id=12, text="Текст", date=date)
        assert post_to_document(post, CHANNEL, 3, CHANNEL_TITLE, now).published_at == expected

    @pytest.mark.parametrize(
        ("moment", "expected"),
        [
            (datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc), "2026-09-02T12:00:00+00:00"),
            (datetime(2026, 9, 2, 13, 0, 0, tzinfo=timezone.utc), "2026-09-02T13:00:00+00:00"),
        ],
        ids=["noon", "an-hour-later"],
    )
    def test_fetched_at_comes_from_the_injected_clock(self, moment, expected):
        post = MtPost(id=13, text="Текст", date=None)
        assert post_to_document(post, CHANNEL, 3, CHANNEL_TITLE, moment).fetched_at == expected

    def test_unsaved_source_id_is_carried_through(self, now):
        post = MtPost(id=14, text="Текст", date=now)
        assert post_to_document(post, CHANNEL, 0, CHANNEL_TITLE, now).source_id == 0


# ── posts_from_messages ────────────────────────────────────────────────────


class TestPostsFromMessages:
    def test_empty_input_gives_no_posts(self):
        assert posts_from_messages([]) == []

    def test_posts_come_back_ordered_by_id_ascending(self, now):
        messages = [FakeMessage(3, text="в"), FakeMessage(1, text="а"), FakeMessage(2, text="б")]
        assert [p.id for p in posts_from_messages(messages)] == [1, 2, 3]
        assert [p.text for p in posts_from_messages(messages)] == ["а", "б", "в"]

    def test_service_messages_are_dropped(self):
        messages = [
            FakeMessage(1, text="Пост"),
            FakeMessage(2, text="", action=object()),  # "channel photo changed" & friends
            FakeMessage(3, text="Ещё пост", action=object()),
        ]
        assert [p.id for p in posts_from_messages(messages)] == [1]

    def test_surrounding_whitespace_is_stripped_from_the_text(self):
        (post,) = posts_from_messages([FakeMessage(1, text="\n  Текст поста \n\n")])
        assert post.text == "Текст поста"

    def test_message_without_text_keeps_an_empty_string(self):
        (post,) = posts_from_messages([FakeMessage(1, text=None, file=FakeFile("x.pdf"))])
        assert post.text == ""

    def test_date_is_taken_from_the_message(self):
        moment = datetime(2026, 8, 18, 10, 31, 52, tzinfo=timezone.utc)
        (post,) = posts_from_messages([FakeMessage(1, text="Текст", date=moment)])
        assert post.date == moment

    # -- albums --

    def test_album_siblings_merge_into_the_post_that_carries_the_caption(self):
        head_date = datetime(2026, 8, 20, 8, 0, 0, tzinfo=timezone.utc)
        messages = [
            FakeMessage(10, grouped_id=777, file=FakeFile("а.pdf"), date=head_date),
            FakeMessage(11, text="Подборка документов", grouped_id=777, file=FakeFile("б.pdf"),
                        date=head_date + timedelta(seconds=1)),
            FakeMessage(12, grouped_id=777, file=FakeFile("в.pdf"),
                        date=head_date + timedelta(seconds=2)),
        ]
        posts = posts_from_messages(messages)
        assert len(posts) == 1
        assert posts[0].id == 10  # the album is numbered by its first message
        assert posts[0].text == "Подборка документов"
        assert posts[0].files == ["а.pdf", "б.pdf", "в.pdf"]
        assert posts[0].date == head_date

    def test_album_merges_urls_without_duplicating_them(self):
        shared = "https://gov.ru/doc"
        messages = [
            FakeMessage(10, text="Каптион", grouped_id=5, entities=[(UrlEntity(), shared)]),
            FakeMessage(11, grouped_id=5, entities=[(UrlEntity(), shared),
                                                    (UrlEntity(), "https://cbr.ru/2")]),
        ]
        (post,) = posts_from_messages(messages)
        assert post.urls == [shared, "https://cbr.ru/2"]

    def test_two_albums_stay_separate(self):
        messages = [
            FakeMessage(1, text="Первый", grouped_id=100, file=FakeFile("a.pdf")),
            FakeMessage(2, grouped_id=100, file=FakeFile("b.pdf")),
            FakeMessage(3, text="Второй", grouped_id=200, file=FakeFile("c.pdf")),
            FakeMessage(4, grouped_id=200, file=FakeFile("d.pdf")),
        ]
        posts = posts_from_messages(messages)
        assert [(p.id, p.files) for p in posts] == [(1, ["a.pdf", "b.pdf"]), (3, ["c.pdf", "d.pdf"])]

    def test_ungrouped_messages_are_never_merged(self):
        messages = [FakeMessage(1, text="Раз"), FakeMessage(2, text="Два")]
        assert [p.text for p in posts_from_messages(messages)] == ["Раз", "Два"]

    # -- urls --

    def test_entity_urls_come_from_both_entity_shapes(self):
        messages = [
            FakeMessage(
                1,
                text="Опубликован приказ",
                entities=[
                    (TextUrlEntity("https://gov.ru/doc/1"), "приказ"),
                    (UrlEntity(), "https://cbr.ru/news/2"),
                ],
            )
        ]
        (post,) = posts_from_messages(messages)
        assert post.urls == ["https://gov.ru/doc/1", "https://cbr.ru/news/2"]

    def test_tme_links_are_excluded(self):
        messages = [
            FakeMessage(
                1,
                text="Репост",
                entities=[
                    (TextUrlEntity("https://t.me/other/1"), "канал"),
                    (UrlEntity(), "https://t.me/s/cit_gov"),
                    (TextUrlEntity("https://gov.ru/keep"), "оставить"),
                ],
                preview_url="https://t.me/other/1",
            )
        ]
        (post,) = posts_from_messages(messages)
        assert post.urls == ["https://gov.ru/keep"]

    def test_web_preview_url_is_included_after_the_entities(self):
        messages = [
            FakeMessage(
                1,
                text="Ссылка",
                entities=[(TextUrlEntity("https://gov.ru/doc"), "документ")],
                preview_url="https://media.ru/story",
            )
        ]
        (post,) = posts_from_messages(messages)
        assert post.urls == ["https://gov.ru/doc", "https://media.ru/story"]

    @pytest.mark.parametrize(
        "text",
        ["mailto:press@gov.ru", "@cit_gov", "tg://resolve?domain=cit_gov", "не ссылка"],
        ids=["mailto", "mention", "tg-scheme", "plain-text"],
    )
    def test_non_http_entities_are_ignored(self, text):
        (post,) = posts_from_messages([FakeMessage(1, text="Текст", entities=[(UrlEntity(), text)])])
        assert post.urls == []

    def test_duplicate_urls_are_deduplicated_in_order(self):
        messages = [
            FakeMessage(
                1,
                text="Текст",
                entities=[
                    (UrlEntity(), "https://gov.ru/1"),
                    (TextUrlEntity("https://gov.ru/1"), "тот же"),
                    (UrlEntity(), "https://gov.ru/2"),
                ],
                preview_url="https://gov.ru/1",
            )
        ]
        (post,) = posts_from_messages(messages)
        assert post.urls == ["https://gov.ru/1", "https://gov.ru/2"]

    @pytest.mark.parametrize(
        "error",
        [AttributeError("'MessageService' object has no attribute 'get_entities_text'"),
         TypeError("cannot unpack non-sequence")],
        ids=["attribute-error", "type-error"],
    )
    def test_message_whose_entities_blow_up_degrades_to_no_urls(self, error):
        (post,) = posts_from_messages([FakeMessage(1, text="Текст", entities_error=error)])
        assert post.urls == []
        assert post.text == "Текст"

    # -- files --

    def test_document_file_name_is_captured(self):
        (post,) = posts_from_messages([FakeMessage(1, text="Текст", file=FakeFile("Отчёт.pdf"))])
        assert post.files == ["Отчёт.pdf"]

    def test_photo_contributes_no_file_name(self):
        (post,) = posts_from_messages([FakeMessage(1, text="Фото дня", file=FakeFile(None))])
        assert post.files == []

    def test_message_without_a_file_attribute_contributes_no_file_name(self):
        (post,) = posts_from_messages([FakeMessage(1, text="Текст")])
        assert post.files == []

    def test_a_photo_only_post_survives_as_a_post_with_nothing_to_read(self, now):
        """It has no document, but its id still moves the cursor."""
        (post,) = posts_from_messages([FakeMessage(1500, text="", file=FakeFile(None))])
        assert (post.id, post.text, post.files) == (1500, "", [])
        assert post_to_document(post, CHANNEL, 3, CHANNEL_TITLE, now) is None


# ── credentials_from_env ───────────────────────────────────────────────────


@pytest.fixture
def paths(tmp_path) -> ProjectPaths:
    return ProjectPaths.from_root(str(tmp_path))


@pytest.fixture
def clean_env(monkeypatch):
    """No Telegram credentials leak in from the developer's shell."""
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)


class TestCredentialsFromEnv:
    def test_both_values_present_build_credentials(self, config, paths, monkeypatch, clean_env):
        monkeypatch.setenv("TELEGRAM_API_ID", "1234567")
        monkeypatch.setenv("TELEGRAM_API_HASH", "0123456789abcdef0123456789abcdef")
        creds = credentials_from_env(config.telegram, paths)
        assert creds == TelegramCredentials(
            api_id=1234567,
            api_hash="0123456789abcdef0123456789abcdef",
            session_path=paths.data("telegram.session"),
        )

    def test_session_path_follows_the_configured_session_name(self, config, paths, monkeypatch,
                                                              clean_env):
        monkeypatch.setenv("TELEGRAM_API_ID", "1")
        monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
        tg = replace(config.telegram, session_name="work")
        creds = credentials_from_env(tg, paths)
        assert creds.session_path == paths.data("work.session")

    def test_custom_env_var_names_are_honoured(self, config, paths, monkeypatch, clean_env):
        monkeypatch.setenv("TG_ID", "77")
        monkeypatch.setenv("TG_HASH", "hash")
        tg = replace(config.telegram, api_id_env="TG_ID", api_hash_env="TG_HASH")
        creds = credentials_from_env(tg, paths)
        assert (creds.api_id, creds.api_hash) == (77, "hash")

    @pytest.mark.parametrize(
        "env",
        [{}, {"TELEGRAM_API_ID": "1234567"}, {"TELEGRAM_API_HASH": "hash"},
         {"TELEGRAM_API_ID": "", "TELEGRAM_API_HASH": "hash"},
         {"TELEGRAM_API_ID": "1234567", "TELEGRAM_API_HASH": ""}],
        ids=["neither", "no-hash", "no-id", "blank-id", "blank-hash"],
    )
    def test_missing_or_blank_values_give_none(self, config, paths, monkeypatch, clean_env, env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        assert credentials_from_env(config.telegram, paths) is None

    @pytest.mark.parametrize(
        "raw_id", ["abc", "12 34", " ", "12.5", "1234567abc", "не число"],
        ids=["letters", "space", "blank", "float", "trailing-letters", "cyrillic"],
    )
    def test_a_non_numeric_api_id_gives_none(self, config, paths, monkeypatch, clean_env, raw_id):
        monkeypatch.setenv("TELEGRAM_API_ID", raw_id)
        monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
        assert credentials_from_env(config.telegram, paths) is None

    def test_a_non_numeric_api_id_is_logged(self, config, paths, monkeypatch, clean_env, caplog):
        monkeypatch.setenv("TELEGRAM_API_ID", "не число")
        monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
        with caplog.at_level("WARNING", logger="telegram.mtproto"):
            assert credentials_from_env(config.telegram, paths) is None
        assert "TELEGRAM_API_ID is not a number; MTProto disabled" in caplog.text

    def test_values_are_read_from_the_project_dot_env(self, config, paths, tmp_path, monkeypatch,
                                                      clean_env):
        (tmp_path / ".env").write_text(
            'OTHER=1\nTELEGRAM_API_ID=987654\nTELEGRAM_API_HASH="from-dot-env"\n', encoding="utf-8"
        )
        creds = credentials_from_env(config.telegram, paths)
        assert (creds.api_id, creds.api_hash) == (987654, "from-dot-env")

    def test_the_environment_wins_over_the_dot_env_file(self, config, paths, tmp_path, monkeypatch,
                                                        clean_env):
        (tmp_path / ".env").write_text("TELEGRAM_API_ID=1\nTELEGRAM_API_HASH=file\n",
                                       encoding="utf-8")
        monkeypatch.setenv("TELEGRAM_API_HASH", "env")
        creds = credentials_from_env(config.telegram, paths)
        assert (creds.api_id, creds.api_hash) == (1, "env")


# ── delete_session ─────────────────────────────────────────────────────────


class TestDeleteSession:
    @staticmethod
    def _creds(tmp_path) -> TelegramCredentials:
        return TelegramCredentials(api_id=1, api_hash="h",
                                   session_path=str(tmp_path / "telegram.session"))

    def test_removes_the_session_and_its_journal(self, tmp_path):
        creds = self._creds(tmp_path)
        journal = f"{creds.session_path}-journal"
        for path in (creds.session_path, journal):
            with open(path, "wb") as f:
                f.write(b"sqlite")
        assert delete_session(creds) == [creds.session_path, journal]
        assert not os.path.exists(creds.session_path)
        assert not os.path.exists(journal)

    def test_removes_the_session_alone_when_there_is_no_journal(self, tmp_path):
        creds = self._creds(tmp_path)
        with open(creds.session_path, "wb") as f:
            f.write(b"sqlite")
        assert delete_session(creds) == [creds.session_path]

    def test_is_a_no_op_when_nothing_exists(self, tmp_path):
        creds = self._creds(tmp_path)
        assert delete_session(creds) == []
        assert delete_session(creds) == []  # and stays one on a second call
