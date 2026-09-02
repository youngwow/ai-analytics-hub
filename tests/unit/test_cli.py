"""src/cli.py — argument parsing and command smoke tests (no network, no LLM)."""

from __future__ import annotations

import argparse
import json
import shutil

import pytest

from src import cli
from src.models import CollectReport, RawDocument, Resolution, Source
from src.paths import DEFAULT_PATHS as REAL_PATHS
from src.paths import ProjectPaths
from src.storage import Database


@pytest.fixture
def paths(tmp_path) -> ProjectPaths:
    """A throwaway project root with the real config.yaml copied in."""
    shutil.copy(REAL_PATHS.config_path, tmp_path / "config.yaml")
    return ProjectPaths.from_root(str(tmp_path))


def _args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


# ── parser ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["collect"],
            {"func": cli._cmd_collect, "source": None, "backfill": False, "force": False,
             "watch": False, "interval": 900},
        ),
        (
            ["collect", "--source", "1", "--source", "2", "--backfill", "--force", "--watch",
             "--interval", "60"],
            {"source": [1, 2], "backfill": True, "force": True, "watch": True, "interval": 60},
        ),
        (["sources", "list"], {"func": cli._cmd_sources_list, "action": "list"}),
        (
            ["sources", "add", "https://a.ru", "--name", "A", "--category", "regulator",
             "--kind", "rss", "--fetch-url", "https://a.ru/rss"],
            {"func": cli._cmd_sources_add, "url": "https://a.ru", "name": "A",
             "category": "regulator", "kind": "rss", "fetch_url": "https://a.ru/rss"},
        ),
        (["sources", "add", "https://a.ru"],
         {"name": None, "category": None, "kind": None, "fetch_url": None}),
        (["sources", "enable", "3"], {"func": cli._cmd_sources_toggle, "id": 3, "enable": True}),
        (["sources", "disable", "3"], {"func": cli._cmd_sources_toggle, "id": 3, "enable": False}),
        (["sources", "remove", "4"], {"func": cli._cmd_sources_remove, "id": 4}),
        (["sources", "resolve", "5"], {"func": cli._cmd_sources_resolve, "id": 5}),
        (["sources", "seed"], {"func": cli._cmd_sources_seed, "file": None}),
        (["sources", "seed", "x.json"], {"func": cli._cmd_sources_seed, "file": "x.json"}),
        (["resolve", "https://a.ru"], {"func": cli._cmd_resolve, "url": "https://a.ru"}),
        (
            ["discover", "закон об ИИ", "--domains", "gov.ru,cbr.ru", "--max", "5", "--news",
             "--add"],
            {"func": cli._cmd_discover, "query": "закон об ИИ", "domains": "gov.ru,cbr.ru",
             "max": 5, "news": True, "add": True},
        ),
        (["discover", "q"], {"domains": None, "max": None, "news": False, "add": False}),
        (["import-url", "https://a.ru/x"], {"func": cli._cmd_import_url, "url": "https://a.ru/x"}),
        (["docs"], {"func": cli._cmd_docs, "source": None, "limit": 20}),
        (["docs", "--source", "1", "--limit", "5"], {"source": 1, "limit": 5}),
    ],
    ids=lambda v: " ".join(v) if isinstance(v, list) else "",
)
def test_build_parser_parses_every_command(argv, expected):
    args = cli.build_parser().parse_args(argv)
    for key, value in expected.items():
        assert getattr(args, key) == value, key


@pytest.mark.parametrize(
    "argv",
    [[], ["sources"], ["bogus"], ["sources", "add", "u", "--kind", "bogus"],
     ["sources", "add", "u", "--category", "bogus"], ["sources", "enable", "x"], ["collect", "--nope"]],
    ids=lambda v: " ".join(v) or "<empty>",
)
def test_build_parser_rejects_bad_input(argv):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)


# ── main ───────────────────────────────────────────────────────────────────


def test_main_docs_on_empty_project(monkeypatch, capsys, paths):
    monkeypatch.setattr(cli, "DEFAULT_PATHS", paths)
    assert cli.main(["docs"]) == 0
    out = capsys.readouterr().out
    assert "no documents yet" in out
    assert "(0 documents total)" in out


def test_main_returns_2_when_config_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "DEFAULT_PATHS", ProjectPaths.from_root(str(tmp_path)))
    assert cli.main(["docs"]) == 2


# ── collect ────────────────────────────────────────────────────────────────


def test_collect_watch_stops_cleanly_on_keyboard_interrupt(config, paths, capsys):
    calls: list[int] = []

    def sleep(seconds):
        calls.append(seconds)
        raise KeyboardInterrupt

    args = _args(source=None, backfill=False, force=False, watch=True, interval=42)
    assert cli._cmd_collect(args, config, paths, sleep=sleep) == 0
    assert calls == [42]
    out = capsys.readouterr().out
    assert out.count("new documents;") == 1
    assert out.strip() == "0 new documents; sources ok=0 not_modified=0 failed=0"


def test_collect_without_watch_runs_once_and_returns_zero(config, paths, capsys):
    args = _args(source=None, backfill=False, force=False, watch=False, interval=900)
    assert cli._cmd_collect(args, config, paths, sleep=lambda s: pytest.fail("must not sleep")) == 0
    assert capsys.readouterr().out.startswith("0 new documents")
    db = Database(paths.db_path)
    try:
        assert db.runs.latest() is not None
    finally:
        db.close()


def test_collect_returns_1_when_every_polled_source_failed(monkeypatch, config, paths):
    class StubCollector:
        def __init__(self, *a, **k):
            pass

        def run(self, source_ids=None, backfill=False, force=False):
            return CollectReport(sources_fail=2, per_source=[{"status": "failed"}] * 2)

    monkeypatch.setattr(cli, "Collector", StubCollector)
    args = _args(source=None, backfill=False, force=False, watch=False, interval=900)
    assert cli._cmd_collect(args, config, paths) == 1


# ── sources ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "kind", "expected"),
    [
        ("https://t.me/cit_gov", "telegram", "telegram"),
        ("https://x.ru", "manual", "manual"),
        ("https://digital.gov.ru/ru/events/", "rss", "regulator"),
        ("http://government.ru/all/rss/", "rss", "regulator"),
        ("https://www.cbr.ru/rss/RssPress", "rss", "regulator"),
        ("https://sozd.duma.gov.ru/", "html", "regulator"),
        ("http://www.consultant.ru/", "html", "regulator"),
        ("https://www.vedomosti.ru", "rss", "media"),
        ("https://notgov.ru", "rss", "media"),
        ("https://gov.ru.evil.com", "rss", "media"),
    ],
)
def test_guess_category(url, kind, expected):
    assert cli.guess_category(url, kind) == expected


def test_sources_add_pinned_kind_skips_resolver_and_reports_duplicates(config, paths, capsys):
    args = _args(url="https://digital.gov.ru", name="Минцифры", category=None, kind="rss",
                 fetch_url="https://digital.gov.ru/rss/")
    assert cli._cmd_sources_add(args, config, paths) == 0
    assert capsys.readouterr().out.strip() == (
        "added #1 [rss/regulator] Минцифры → https://digital.gov.ru/rss/"
    )
    assert cli._cmd_sources_add(args, config, paths) == 0
    assert capsys.readouterr().out.startswith("already exists: #1 Минцифры")


def test_sources_add_uses_resolver_when_not_pinned(monkeypatch, config, paths, capsys):
    class StubResolver:
        def __init__(self, client, limiter=None, probe_timeout=8.0):
            pass

        def resolve(self, url):
            return Resolution(kind="sitemap", fetch_url=url + "sitemap.xml", name="Сайт",
                              note="no feed; polling sitemap by lastmod")

    monkeypatch.setattr(cli, "Resolver", StubResolver)
    args = _args(url="https://site.ru/", name=None, category=None, kind=None, fetch_url=None)
    assert cli._cmd_sources_add(args, config, paths) == 0
    out = capsys.readouterr().out
    assert "added #1 [sitemap/media] Сайт → https://site.ru/sitemap.xml" in out
    assert "note: no feed; polling sitemap by lastmod" in out


def test_sources_list_empty_and_populated(config, paths, capsys):
    assert cli._cmd_sources_list(_args(), config, paths) == 0
    assert "no sources yet" in capsys.readouterr().out
    db = Database(paths.db_path)
    db.sources.add(Source(name="Ведомости", url="https://v.ru", kind="rss", category="media",
                          fetch_url="https://v.ru/rss"))
    db.close()
    assert cli._cmd_sources_list(_args(), config, paths) == 0
    out = capsys.readouterr().out
    assert "Ведомости" in out
    assert out.splitlines()[0].split()[:3] == ["id", "kind", "category"]


def test_sources_toggle_and_remove(config, paths, capsys):
    db = Database(paths.db_path)
    source = db.sources.add(Source(name="A", url="https://a.ru", kind="rss", category="media",
                                   fetch_url="https://a.ru/rss"))
    db.close()
    assert cli._cmd_sources_toggle(_args(id=source.id, enable=False), config, paths) == 0
    assert capsys.readouterr().out.strip() == f"source #{source.id} disabled"
    assert cli._cmd_sources_toggle(_args(id=999, enable=True), config, paths) == 1
    assert cli._cmd_sources_remove(_args(id=source.id), config, paths) == 0
    assert capsys.readouterr().out.strip() == f"removed source #{source.id} and its 0 documents"
    assert cli._cmd_sources_remove(_args(id=source.id), config, paths) == 1


def test_sources_resolve_updates_kind_and_resets_cursor(monkeypatch, config, paths, capsys):
    db = Database(paths.db_path)
    source = db.sources.add(Source(name="A", url="https://a.ru", kind="html", category="media",
                                   fetch_url="https://a.ru"))
    with db.transaction():
        from src.models import FetchState

        db.fetch_state.save(FetchState(source_id=source.id, etag="e", cursor={"x": 1}))
    db.close()

    class StubResolver:
        def __init__(self, client, limiter=None, probe_timeout=8.0):
            pass

        def resolve(self, url):
            return Resolution(kind="rss", fetch_url="https://a.ru/rss", note="found")

    monkeypatch.setattr(cli, "Resolver", StubResolver)
    assert cli._cmd_sources_resolve(_args(id=source.id), config, paths) == 0
    assert capsys.readouterr().out.strip().endswith("rss → https://a.ru/rss (changed, cursor reset)")
    db = Database(paths.db_path)
    try:
        stored = db.sources.get(source.id)
        assert (stored.kind, stored.fetch_url, stored.notes) == ("rss", "https://a.ru/rss", "found")
        assert db.fetch_state.get(source.id).etag is None
        assert db.fetch_state.get(source.id).cursor == {}
    finally:
        db.close()
    assert cli._cmd_sources_resolve(_args(id=999), config, paths) == 1


def test_sources_seed_from_json(config, paths, tmp_path, capsys):
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps(
            {
                "sources": [
                    {"name": "Банк России", "url": "https://www.cbr.ru", "kind": "rss",
                     "fetch_url": "https://www.cbr.ru/rss/RssPress", "notes": "полный текст"},
                    {"url": ""},
                ],
                "excluded": [{"name": "Закрытый канал", "reason": "нет превью"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert cli._cmd_sources_seed(_args(file=str(seed)), config, paths) == 0
    out = capsys.readouterr().out
    assert "+ #1 [rss/regulator] Банк России → https://www.cbr.ru/rss/RssPress" in out
    assert "- skipped Закрытый канал: нет превью" in out
    assert "seed: 1 added, 0 already present, 0 failed" in out

    assert cli._cmd_sources_seed(_args(file=str(seed)), config, paths) == 0
    out = capsys.readouterr().out
    assert "= #1" in out
    assert "seed: 0 added, 1 already present, 0 failed" in out


def test_sources_seed_missing_file(config, paths, capsys):
    assert cli._cmd_sources_seed(_args(file=None), config, paths) == 1
    assert "cannot read seed file" in capsys.readouterr().err


# ── resolve / discover / import / docs ─────────────────────────────────────


def test_resolve_command_prints_resolution(monkeypatch, config, paths, capsys):
    class StubResolver:
        def __init__(self, client, limiter=None, probe_timeout=8.0):
            pass

        def resolve(self, url):
            return Resolution(kind="telegram", fetch_url="https://t.me/s/cit_gov", name="cit_gov")

    monkeypatch.setattr(cli, "Resolver", StubResolver)
    assert cli._cmd_resolve(_args(url="t.me/cit_gov"), config, paths) == 0
    assert capsys.readouterr().out.splitlines() == [
        "kind:      telegram",
        "fetch_url: https://t.me/s/cit_gov",
        "name:      cit_gov",
    ]


def test_discover_without_api_key_returns_2(monkeypatch, config, paths, capsys):
    monkeypatch.delenv("TAVILY_API", raising=False)
    args = _args(query="закон об ИИ", domains=None, max=None, news=False, add=False)
    assert cli._cmd_discover(args, config, paths) == 2
    assert "no Tavily API key" in capsys.readouterr().err


def test_import_url_command(monkeypatch, config, paths, capsys):
    class StubCollector:
        def __init__(self, *a, **k):
            pass

        def import_url(self, url):
            if url.endswith("/bad"):
                raise ValueError(f"could not extract anything readable from {url}")
            return 5, not url.endswith("/again")

    monkeypatch.setattr(cli, "Collector", StubCollector)
    assert cli._cmd_import_url(_args(url="https://a.ru/x"), config, paths) == 0
    assert capsys.readouterr().out.strip() == "imported as document #5"
    assert cli._cmd_import_url(_args(url="https://a.ru/again"), config, paths) == 0
    assert capsys.readouterr().out.strip() == "already stored as document #5"
    assert cli._cmd_import_url(_args(url="https://a.ru/bad"), config, paths) == 1
    assert "could not extract" in capsys.readouterr().err


def test_docs_lists_stored_documents(config, paths, capsys):
    db = Database(paths.db_path)
    source = db.sources.add(Source(name="Источник", url="https://a.ru", kind="rss",
                                   category="media", fetch_url="https://a.ru/rss"))
    with db.transaction():
        db.documents.insert(
            RawDocument(source.id, "one", "https://a.ru/one", title="Первый документ",
                        text="абв", published_at="2026-09-02T07:00:00+00:00",
                        fetched_at="2026-09-02T12:00:00+00:00")
        )
    db.close()
    assert cli._cmd_docs(_args(source=None, limit=20), config, paths) == 0
    out = capsys.readouterr().out
    assert "Первый документ" in out
    assert "2026-09-02T07:00" in out
    assert "(1 documents total)" in out


def test_table_pads_columns():
    text = cli._table(["id", "name"], [["1", "Ведомости"], ["22", "К"]])
    lines = text.splitlines()
    assert lines[0] == "id  name     "
    assert lines[1] == "--  ---------"
    assert lines[2] == "1   Ведомости"
    assert lines[3] == "22  К        "
