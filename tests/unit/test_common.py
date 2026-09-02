"""src/common.py — clock conversions, hashing, .env secrets."""

from __future__ import annotations

import hashlib
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from src.common import (
    MOSCOW,
    load_env_secret,
    normalize_ws,
    parse_datetime,
    sha256_text,
    struct_time_to_datetime,
    to_utc_iso,
)

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))


# ── to_utc_iso ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (datetime(2026, 9, 2, 12, 30, tzinfo=PLUS3), "2026-09-02T09:30:00+00:00"),
        (datetime(2026, 9, 2, 12, 0, tzinfo=UTC), "2026-09-02T12:00:00+00:00"),
        (datetime(2026, 9, 2, 12, 0), "2026-09-02T09:00:00+00:00"),
        (date(2026, 9, 2), "2026-09-01T21:00:00+00:00"),
        (datetime(2026, 9, 2, 12, 0, 0, 123456, tzinfo=UTC), "2026-09-02T12:00:00+00:00"),
        (datetime(2026, 1, 15, 0, 30, tzinfo=MOSCOW), "2026-01-14T21:30:00+00:00"),
    ],
    ids=["none", "aware+3", "aware-utc", "naive-is-moscow", "date-is-midnight-moscow",
         "microseconds-dropped", "moscow-winter-still-utc+3"],
)
def test_to_utc_iso(value, expected):
    assert to_utc_iso(value) == expected


def test_to_utc_iso_is_fixed_width():
    assert len(to_utc_iso(datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC))) == 25


# ── parse_datetime ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected_utc"),
    [
        ("2026-09-02T12:30:00+03:00", datetime(2026, 9, 2, 9, 30, tzinfo=UTC)),
        ("2026-09-02T12:30:00+0300", datetime(2026, 9, 2, 9, 30, tzinfo=UTC)),
        ("2026-09-02T09:30:00Z", datetime(2026, 9, 2, 9, 30, tzinfo=UTC)),
        ("2026-09-02T12:30:00", datetime(2026, 9, 2, 9, 30, tzinfo=UTC)),
        ("2026-09-02", datetime(2026, 9, 1, 21, 0, tzinfo=UTC)),
        ("Tue, 02 Sep 2026 10:00:00 +0300", datetime(2026, 9, 2, 7, 0, tzinfo=UTC)),
        ("Mon, 01 Sep 2026 10:00:00 GMT", datetime(2026, 9, 1, 10, 0, tzinfo=UTC)),
        ("  2026-09-02T09:30:00Z  ", datetime(2026, 9, 2, 9, 30, tzinfo=UTC)),
    ],
    ids=["iso-offset", "iso-offset-no-colon", "iso-z", "iso-naive-moscow", "date-only",
         "rfc2822", "rfc2822-gmt", "padded"],
)
def test_parse_datetime_returns_aware_datetime(value, expected_utc):
    parsed = parse_datetime(value)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed == expected_utc


def test_parse_datetime_keeps_original_offset():
    assert parse_datetime("2026-09-02T12:30:00+03:00").utcoffset() == timedelta(hours=3)


def test_parse_datetime_naive_gets_moscow_zone():
    assert parse_datetime("2026-09-02T12:30:00").tzinfo is MOSCOW


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "garbage", "32.13.2026", "вчера", "2026-13-45"],
    ids=["none", "empty", "blank", "word", "dd.mm.yyyy-invalid", "cyrillic", "bad-iso"],
)
def test_parse_datetime_returns_none_for_unparseable(value):
    assert parse_datetime(value) is None


# ── struct_time_to_datetime ────────────────────────────────────────────────


def test_struct_time_to_datetime_is_utc():
    st = time.struct_time((2026, 9, 2, 7, 0, 0, 2, 245, 0))
    assert struct_time_to_datetime(st) == datetime(2026, 9, 2, 7, 0, tzinfo=UTC)
    assert struct_time_to_datetime(st).tzinfo is UTC


@pytest.mark.parametrize("value", [None, (), ("a", "b", "c", "d", "e", "f"), (2026, 13, 1, 0, 0, 0)])
def test_struct_time_to_datetime_tolerates_junk(value):
    assert struct_time_to_datetime(value) is None


# ── normalize_ws / sha256_text ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [("  a \n\t b  ", "a b"), ("", ""), (None, ""), ("один два", "один два")],
)
def test_normalize_ws(value, expected):
    assert normalize_ws(value) == expected


def test_sha256_text_ignores_whitespace_differences():
    assert sha256_text("Заголовок  статьи", "текст\n\nтекст") == sha256_text(
        " Заголовок статьи ", "текст текст"
    )


def test_sha256_text_is_sha256_of_normalised_parts_joined_by_newline():
    expected = hashlib.sha256("a b\nc".encode("utf-8")).hexdigest()
    assert sha256_text("a   b", " c ") == expected


def test_sha256_text_differs_when_a_part_changes():
    assert sha256_text("a", "b") != sha256_text("a", "c")
    assert sha256_text("a", "b") != sha256_text("a b")


# ── load_env_secret ────────────────────────────────────────────────────────


def test_load_env_secret_prefers_process_environment(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TAVILY_API=from-file\n", encoding="utf-8")
    monkeypatch.setenv("TAVILY_API", "from-env")
    assert load_env_secret("TAVILY_API", str(env_file)) == "from-env"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('TAVILY_API="quoted-key"', "quoted-key"),
        ("TAVILY_API='single'", "single"),
        ("TAVILY_API=plain", "plain"),
        ("TAVILY_API=", ""),
    ],
    ids=["double-quotes", "single-quotes", "plain", "empty-value"],
)
def test_load_env_secret_reads_dotenv_file(monkeypatch, tmp_path, line, expected):
    monkeypatch.delenv("TAVILY_API", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"# comment\nOTHER=1\n{line}\n", encoding="utf-8")
    assert load_env_secret("TAVILY_API", str(env_file)) == expected


def test_load_env_secret_returns_empty_when_unset_and_no_file(monkeypatch, tmp_path):
    monkeypatch.delenv("TAVILY_API", raising=False)
    assert load_env_secret("TAVILY_API", str(tmp_path / "missing.env")) == ""
    assert load_env_secret("TAVILY_API", None) == ""
