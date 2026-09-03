"""src/cli.py — argument parsing and command smoke tests (no network, no LLM)."""

from __future__ import annotations

import argparse
import json
import shutil
from contextlib import contextmanager

import pytest
from support import JSON, MockRoutes

from src import cli
from src.config import Config
from src.models import CollectReport, RawDocument, Resolution, Source
from src.paths import DEFAULT_PATHS as REAL_PATHS
from src.paths import ProjectPaths
from src.sources.collector import Collector
from src.sources.scraper_llm import TAVILY_SEARCH_URL
from src.sources.scraper_search import SearchQuery
from src.storage import Database

SEARCH_QUERY = "GS Labs Триколор"


@pytest.fixture
def paths(tmp_path) -> ProjectPaths:
    """A throwaway project root with the real config.yaml copied in."""
    shutil.copy(REAL_PATHS.config_path, tmp_path / "config.yaml")
    return ProjectPaths.from_root(str(tmp_path))


@pytest.fixture
def tavily_routes(fixture_bytes) -> MockRoutes:
    return MockRoutes({TAVILY_SEARCH_URL: (200, fixture_bytes("tavily_news.json"), JSON)})


@pytest.fixture
def offline_collector(monkeypatch, tavily_routes, now) -> MockRoutes:
    """`cli.Collector` bound to the mock transport and the frozen clock.

    The Tavily key is deliberately *not* injected: `_cmd_search` must find it
    in the environment or the project `.env`, exactly as in production.
    """

    class OfflineCollector(Collector):
        def __init__(self, config, paths, db, **kwargs):
            tavily_routes.collector_kwargs.append(dict(kwargs))
            super().__init__(
                config, paths, db, transport=tavily_routes.transport(), now=lambda: now, **kwargs
            )

    tavily_routes.collector_kwargs = []  # what `_cmd_search` passed besides (config, paths, db)
    monkeypatch.setattr(cli, "Collector", OfflineCollector)
    return tavily_routes


@pytest.fixture
def tavily_key(monkeypatch) -> str:
    monkeypatch.setenv("TAVILY_API", "secret-key")
    return "secret-key"


def _args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _search_args(query: str = SEARCH_QUERY, **overrides) -> argparse.Namespace:
    base = dict(query=query, domains=None, days=None, max=None, category=None, general=False,
                no_summary=False, save=False, name=None)
    return _args(**{**base, **overrides})


def _rows(out: str) -> list[str]:
    """The hit rows of a `search` table: they start with the `+`/`=` mark column."""
    return [line for line in out.splitlines() if line[:3] in ("+  ", "=  ")]


def _payloads(routes: MockRoutes) -> list[dict]:
    return [json.loads(r.content) for r in routes.requests_to(TAVILY_SEARCH_URL)]


@contextmanager
def _db(paths: ProjectPaths):
    db = Database(paths.db_path)
    try:
        yield db
    finally:
        db.close()


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
        (["discover", "q", "--domains", "@regulator"], {"domains": "@regulator"}),
        (
            ["search", "закон об ИИ", "--domains", "@media", "--days", "3", "--max", "5",
             "--category", "regulator", "--general", "--no-summary", "--save", "--name", "ИИ"],
            {"func": cli._cmd_search, "query": "закон об ИИ", "domains": "@media", "days": 3,
             "max": 5, "category": "regulator", "general": True, "no_summary": True,
             "save": True, "name": "ИИ"},
        ),
        (
            ["search", "q"],
            {"func": cli._cmd_search, "query": "q", "domains": None, "days": None, "max": None,
             "category": None, "general": False, "no_summary": False, "save": False,
             "name": None},
        ),
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
     ["sources", "add", "u", "--category", "bogus"], ["sources", "enable", "x"], ["collect", "--nope"],
     ["discover", "q", "--collect"], ["search"], ["search", "q", "--days", "week"],
     ["search", "q", "--category", "telegram"], ["search", "q", "--category", "manual"]],
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
        ("tavily://search?q=%D0%B7%D0%B0%D0%BA%D0%BE%D0%BD", "search", "media"),
        ("https://digital.gov.ru/", "search", "media"),
    ],
)
def test_guess_category(url, kind, expected):
    assert cli.guess_category(url, kind) == expected


@pytest.fixture
def domain_sources(db) -> Database:
    """Enabled media/regulator sites plus every kind that must never become a domain."""
    for name, url, kind, category, enabled in [
        ("Ведомости", "https://www.vedomosti.ru", "rss", "media", True),
        ("ЦБ", "https://www.cbr.ru", "rss", "regulator", True),
        ("Дума", "http://duma.gov.ru/", "html", "regulator", True),
        ("Канал", "https://t.me/cit_gov", "telegram", "telegram", True),
        ("Выключен", "https://old.ru", "html", "media", False),
        ("Поиск", SearchQuery("q").to_url(), "search", "media", True),
    ]:
        db.sources.add(Source(name=name, url=url, kind=kind, category=category,
                              fetch_url=url, enabled=enabled))
    db.sources.ensure_manual()
    return db


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (None, []),
        ("", []),
        (" , ", []),
        ("gov.ru, WWW.CBR.ru ,,gov.ru", ["gov.ru", "cbr.ru"]),
        ("@media", ["vedomosti.ru"]),
        ("@regulator", ["cbr.ru", "duma.gov.ru"]),
        ("@all", ["vedomosti.ru", "cbr.ru", "duma.gov.ru"]),
        ("@ALL", ["vedomosti.ru", "cbr.ru", "duma.gov.ru"]),
        ("@regulator,extra.ru,cbr.ru", ["cbr.ru", "duma.gov.ru", "extra.ru"]),
    ],
    ids=["none", "empty", "commas-only", "literal-list", "media", "regulator", "all",
         "preset-case", "preset-plus-literal"],
)
def test_expand_domains(domain_sources, spec, expected):
    assert cli._expand_domains(domain_sources, spec) == expected


def test_expand_domains_on_an_empty_database(db):
    assert cli._expand_domains(db, "@all") == []
    assert cli._expand_domains(db, "@all,gov.ru") == ["gov.ru"]


@pytest.mark.parametrize("spec", ["@nope", "gov.ru,@nope", "@Nope", "@"], ids=lambda s: s)
def test_expand_domains_rejects_an_unknown_preset(domain_sources, spec):
    bad = next(item.strip() for item in spec.split(",") if item.strip().startswith("@"))
    with pytest.raises(
        ValueError, match=rf"^unknown domain preset '{bad}'; use @media, @regulator, @all$"
    ):
        cli._expand_domains(domain_sources, spec)


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


def test_sources_seed_honours_enabled_false_for_new_sources_only(config, paths, tmp_path, capsys):
    search_url = SearchQuery("ИИ в госсекторе", ["gov.ru"]).to_url()
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps(
            {
                "sources": [
                    {"name": "Поиск: ИИ", "url": search_url, "kind": "search",
                     "fetch_url": search_url, "enabled": False},
                    {"name": "Банк России", "url": "https://www.cbr.ru", "kind": "rss",
                     "fetch_url": "https://www.cbr.ru/rss/RssPress", "enabled": True},
                    {"name": "Ведомости", "url": "https://www.vedomosti.ru", "kind": "rss",
                     "fetch_url": "https://www.vedomosti.ru/rss/news"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert cli._cmd_sources_seed(_args(file=str(seed)), config, paths) == 0
    out = capsys.readouterr().out
    assert f"+ #1 [search/media] Поиск: ИИ → {search_url}  (off)" in out
    assert "+ #2 [rss/regulator] Банк России → https://www.cbr.ru/rss/RssPress\n" in out
    assert "+ #3 [rss/media] Ведомости → https://www.vedomosti.ru/rss/news\n" in out
    assert "seed: 3 added, 0 already present, 0 failed" in out
    with _db(paths) as db:
        assert [s.enabled for s in db.sources.list()] == [False, True, True]
        db.sources.set_enabled(1, True)

    # an existing source is reported, never toggled back off by the seed file
    assert cli._cmd_sources_seed(_args(file=str(seed)), config, paths) == 0
    out = capsys.readouterr().out
    assert f"= #1 [search/media] Поиск: ИИ → {search_url}\n" in out
    assert "(off)" not in out
    with _db(paths) as db:
        assert db.sources.get(1).enabled is True


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


@pytest.fixture
def discover_routes(monkeypatch, fixture_bytes) -> MockRoutes:
    """Route `cli.make_client` (used by discover and the resolver) through MockTransport."""
    routes = MockRoutes({TAVILY_SEARCH_URL: (200, fixture_bytes("tavily_search.json"), JSON)})
    monkeypatch.setattr(cli, "make_client", lambda config, transport=None: routes.client())
    return routes


@pytest.mark.parametrize(
    ("news", "topic", "country"),
    [(True, "news", {}), (False, "general", {"country": "russia"})],
    ids=["news", "general"],
)
def test_discover_lists_hits_and_shapes_the_request(raw_config, paths, capsys, discover_routes,
                                                    tavily_key, news, topic, country):
    raw_config["tavily"].update(country="russia", language="ru")
    config = Config.from_dict(raw_config)
    with _db(paths) as db:
        db.sources.add(Source(name="ЦБ", url="https://www.cbr.ru", kind="rss",
                              category="regulator", fetch_url="https://www.cbr.ru/rss/RssPress"))
    args = _args(query="льготы для ИТ-компаний", domains="@regulator,gov.ru", max=5, news=news,
                 add=False)
    assert cli._cmd_discover(args, config, paths) == 0

    payload = _payloads(discover_routes)[0]
    assert payload["query"] == "льготы для ИТ-компаний"
    assert payload["topic"] == topic
    assert payload["max_results"] == 5
    assert payload["include_domains"] == ["cbr.ru", "gov.ru"]
    assert payload["include_answer"] is False
    assert payload["include_raw_content"] is False
    assert payload["language"] == "ru"
    assert {k: payload[k] for k in ("country",) if k in payload} == country
    assert discover_routes.requests[0].headers["authorization"] == "Bearer secret-key"

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["score", "title", "url"]
    assert len(lines) == 4
    assert lines[2].split(maxsplit=1)[0] == "0.91"
    assert "Льготы для ИТ-компаний в 2026 году — Минцифры" in lines[2]
    assert lines[2].rstrip().endswith("https://digital.gov.ru/ru/events/12345/")
    assert lines[3].split(maxsplit=1)[0] == "0.85"
    assert lines[3].rstrip().endswith("http://duma.gov.ru/news/64000/")
    with _db(paths) as db:
        assert len(db.sources.list()) == 1  # no --add: nothing stored


def test_discover_add_stores_each_hit_site_through_the_resolver(config, paths, capsys,
                                                                discover_routes, tavily_key):
    args = _args(query="льготы для ИТ-компаний", domains=None, max=None, news=False, add=True)
    assert cli._cmd_discover(args, config, paths) == 0
    out = capsys.readouterr().out
    assert "+ #1 [html] https://digital.gov.ru → https://digital.gov.ru" in out
    assert "+ #2 [html] http://duma.gov.ru → http://duma.gov.ru" in out
    assert discover_routes.urls()[1:] == ["https://digital.gov.ru", "http://duma.gov.ru"]
    with _db(paths) as db:
        stored = db.sources.list()
        assert [(s.url, s.kind, s.category) for s in stored] == [
            ("https://digital.gov.ru", "html", "regulator"),
            ("http://duma.gov.ru", "html", "regulator"),
        ]
        assert all(s.notes == "unreachable now: HTTP 404" for s in stored)


def test_discover_prints_no_results_on_an_empty_reply(config, paths, capsys, discover_routes,
                                                      tavily_key):
    discover_routes[TAVILY_SEARCH_URL] = (200, b'{"results": []}', JSON)
    args = _args(query="q", domains=None, max=None, news=False, add=True)
    assert cli._cmd_discover(args, config, paths) == 0
    assert capsys.readouterr().out.strip() == "no results"
    with _db(paths) as db:
        assert db.sources.list() == []


def test_discover_returns_2_when_tavily_fails(config, paths, capsys, discover_routes, tavily_key):
    discover_routes[TAVILY_SEARCH_URL] = (429, b"quota", {})
    args = _args(query="q", domains=None, max=None, news=False, add=False)
    assert cli._cmd_discover(args, config, paths) == 2
    assert capsys.readouterr().err.strip() == "Tavily HTTP 429: quota"


def test_discover_rejects_an_unknown_domain_preset_before_calling_tavily(
    config, paths, capsys, discover_routes, tavily_key
):
    args = _args(query="q", domains="@nope", max=None, news=False, add=True)
    assert cli._cmd_discover(args, config, paths) == 1
    assert capsys.readouterr().err.strip() == (
        "unknown domain preset '@nope'; use @media, @regulator, @all"
    )
    assert discover_routes.requests == []
    with _db(paths) as db:
        assert db.sources.list() == []


# ── search ─────────────────────────────────────────────────────────────────


def test_search_stores_hits_and_digest_and_leaves_the_source_off(config, paths, capsys,
                                                                 offline_collector, tavily_key):
    assert cli._cmd_search(_search_args(), config, paths) == 0
    out = capsys.readouterr().out

    rows = _rows(out)
    assert len(rows) == 3
    assert all(row.startswith("+") for row in rows)
    assert "2026-09-01" in rows[0] and "DDR пятого поколения" in rows[0]
    assert "2026-08-31" in rows[1] and "Телеспутник" in rows[1]
    assert rows[2].startswith("+  -") and "Спутниковое ТВ" in rows[2]
    assert "\nСводка: GS Labs Триколор\n«Триколор» совместно с технологическим партнёром" in out
    assert "3 hits, 4 new document(s) → source #1 «GS Labs Триколор» [off]" in out
    assert "hint: `sources enable 1` keeps polling this query with `collect`" in out

    request = offline_collector.requests_to(TAVILY_SEARCH_URL)[0]
    assert request.headers["authorization"] == "Bearer secret-key"
    assert offline_collector.collector_kwargs == [{"tavily_key": "secret-key"}]  # passed explicitly
    payload = json.loads(request.content)
    assert payload["days"] == config.tavily.days == 7
    assert payload["query"] == SEARCH_QUERY
    assert (payload["topic"], payload["include_answer"], payload["include_raw_content"]) == (
        "news", "advanced", "text"
    )
    assert (payload["max_results"], payload["include_domains"]) == (20, [])

    with _db(paths) as db:
        source = db.sources.get(1)
        assert (source.kind, source.category, source.name, source.enabled) == (
            "search", "media", SEARCH_QUERY, False
        )
        assert source.fetch_url == source.url == SearchQuery(SEARCH_QUERY).to_url()
        assert source.notes == "Tavily: news, 7 дн."
        assert db.documents.count(1) == 4
        assert db.documents.exists(1, "summary:2026-09-02")
        assert db.runs.latest()["docs_new"] == 4


def test_search_options_shape_the_query_and_the_source(config, paths, capsys, offline_collector,
                                                       tavily_key):
    args = _search_args(domains="gov.ru, www.CBR.ru", days=3, general=True, no_summary=True,
                        category="regulator", name="ИИ-поиск", save=True)
    assert cli._cmd_search(args, config, paths) == 0

    payload = _payloads(offline_collector)[0]
    assert payload["topic"] == "general"
    assert payload["start_date"] == "2026-08-30"
    assert "days" not in payload
    assert payload["include_domains"] == ["cbr.ru", "gov.ru"]
    assert payload["include_answer"] is False

    out = capsys.readouterr().out
    assert "Сводка:" not in out
    assert "3 hits, 3 new document(s) → source #1 «ИИ-поиск» [on]" in out
    assert "hint:" not in out
    with _db(paths) as db:
        source = db.sources.get(1)
        assert (source.name, source.category, source.enabled) == ("ИИ-поиск", "regulator", True)
        assert source.fetch_url == SearchQuery(SEARCH_QUERY, ["gov.ru", "cbr.ru"], 3, "general",
                                               False).to_url()
        assert source.notes == "Tavily: general, 3 дн., домены: cbr.ru, gov.ru, без сводки"
        assert db.documents.count(1) == 3
        assert not db.documents.exists(1, "summary:2026-09-02")


def test_search_rerun_marks_known_hits_and_save_enables_the_existing_source(
    config, paths, capsys, offline_collector, tavily_key
):
    assert cli._cmd_search(_search_args(), config, paths) == 0
    capsys.readouterr()
    assert cli._cmd_search(_search_args(save=True), config, paths) == 0
    out = capsys.readouterr().out
    assert [row[0] for row in _rows(out)] == ["=", "=", "="]
    assert "3 hits, 0 new document(s) → source #1 «GS Labs Триколор» [on]" in out
    assert "hint:" not in out
    payloads = _payloads(offline_collector)
    assert [p.get("days") for p in payloads] == [7, 7]  # force=True: the cursor never narrows it
    assert all("start_date" not in p for p in payloads)
    with _db(paths) as db:
        assert db.sources.get(1).enabled is True
        assert len(db.sources.list()) == 1
        assert db.documents.count(1) == 4
        assert db.conn.execute("SELECT count(*) FROM collect_runs").fetchone()[0] == 2


def test_search_rerun_without_save_does_not_disable_a_saved_source(config, paths, capsys,
                                                                   offline_collector, tavily_key):
    assert cli._cmd_search(_search_args(save=True), config, paths) == 0
    capsys.readouterr()
    assert cli._cmd_search(_search_args(), config, paths) == 0
    assert "[on]" in capsys.readouterr().out
    with _db(paths) as db:
        assert db.sources.get(1).enabled is True


def test_search_max_limits_printed_rows_not_the_counts(config, paths, capsys, offline_collector,
                                                       tavily_key):
    assert cli._cmd_search(_search_args(max=2), config, paths) == 0
    out = capsys.readouterr().out
    assert len(_rows(out)) == 2
    assert "3 hits, 4 new document(s)" in out
    with _db(paths) as db:
        assert db.documents.count(1) == 4


def test_search_without_api_key_returns_2_without_calling_tavily(monkeypatch, config, paths,
                                                                  capsys, offline_collector):
    monkeypatch.delenv("TAVILY_API", raising=False)
    assert cli._cmd_search(_search_args(), config, paths) == 2
    assert capsys.readouterr().err.strip() == (
        "search failed: no Tavily API key: set TAVILY_API in the environment or .env"
    )
    assert offline_collector.requests == []
    assert offline_collector.collector_kwargs == []  # never even built
    with _db(paths) as db:
        assert db.sources.list() == []  # no dead `search` source row left behind


def test_search_reads_the_key_from_the_project_dot_env(monkeypatch, config, paths, tmp_path,
                                                       offline_collector):
    monkeypatch.delenv("TAVILY_API", raising=False)
    (tmp_path / ".env").write_text('OTHER=1\nTAVILY_API="from-dot-env"\n', encoding="utf-8")
    assert cli._cmd_search(_search_args(), config, paths) == 0
    request = offline_collector.requests_to(TAVILY_SEARCH_URL)[0]
    assert request.headers["authorization"] == "Bearer from-dot-env"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"query": "   "}, "search query is empty"),
        ({"days": 0}, "days must be >= 1"),
        ({"domains": "gov.ru,@nope"}, "unknown domain preset '@nope'; use @media, @regulator, @all"),
    ],
    ids=["blank-query", "zero-days", "unknown-preset"],
)
def test_search_rejects_a_bad_query_before_touching_tavily(config, paths, capsys,
                                                           offline_collector, tavily_key,
                                                           overrides, message):
    assert cli._cmd_search(_search_args(**overrides), config, paths) == 1
    assert capsys.readouterr().err.strip() == message
    assert offline_collector.requests == []
    with _db(paths) as db:
        assert db.sources.list() == []


def test_search_returns_2_when_tavily_fails(config, paths, capsys, offline_collector, tavily_key):
    offline_collector[TAVILY_SEARCH_URL] = (401, b'{"detail": "bad key"}', JSON)
    assert cli._cmd_search(_search_args(), config, paths) == 2
    assert capsys.readouterr().err.startswith("search failed: Tavily HTTP 401")


def test_search_prints_no_results_on_an_empty_reply(config, paths, capsys, offline_collector,
                                                    tavily_key):
    offline_collector[TAVILY_SEARCH_URL] = (200, b'{"results": [], "answer": ""}', JSON)
    assert cli._cmd_search(_search_args(), config, paths) == 0
    out = capsys.readouterr().out
    assert "no results" in out
    assert "0 hits, 0 new document(s) → source #1" in out


def test_search_expands_domain_presets_from_enabled_sources(config, paths, offline_collector,
                                                            tavily_key):
    with _db(paths) as db:
        db.sources.add(Source(name="Ведомости", url="https://www.vedomosti.ru", kind="rss",
                              category="media", fetch_url="https://www.vedomosti.ru/rss/news"))
        db.sources.add(Source(name="ЦБ", url="https://www.cbr.ru", kind="rss",
                              category="regulator", fetch_url="https://www.cbr.ru/rss/RssPress"))
        db.sources.add(Source(name="Выключен", url="https://old.ru", kind="html", category="media",
                              fetch_url="https://old.ru", enabled=False))
    assert cli._cmd_search(_search_args(domains="@all,extra.ru"), config, paths) == 0
    payload = _payloads(offline_collector)[0]
    assert payload["include_domains"] == ["cbr.ru", "extra.ru", "vedomosti.ru"]


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
