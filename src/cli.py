"""argparse CLI — every command is `_cmd_x(args, config, paths) -> exit code`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.parse import urlsplit

from courlan import get_base_url

from .common import get_logger, load_env_secret
from .config import Config, ConfigError
from .models import CATEGORIES, KINDS, Resolution, Source
from .paths import DEFAULT_PATHS, ProjectPaths
from .sources.base import HostLimiter, make_client
from .sources.collector import Collector
from .sources.resolver import Resolver
from .storage import Database, DuplicateSourceError

log = get_logger("cli")

_REGULATOR_HOSTS = (
    "gov.ru",
    "government.ru",
    "cbr.ru",
    "duma.gov.ru",
    "pravo.gov.ru",
    "consultant.ru",
    "garant.ru",
)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines.extend(fmt.format(*row) for row in rows)
    return "\n".join(lines)


def guess_category(url: str, kind: str) -> str:
    if kind == "telegram":
        return "telegram"
    if kind == "manual":
        return "manual"
    host = (urlsplit(url).hostname or "").lower()
    return (
        "regulator"
        if any(host == h or host.endswith("." + h) for h in _REGULATOR_HOSTS)
        else "media"
    )


def _add_source(
    db: Database,
    config: Config,
    url: str,
    *,
    name: str | None = None,
    category: str | None = None,
    kind: str | None = None,
    fetch_url: str | None = None,
    notes: str = "",
) -> tuple[Source | None, str]:
    """Resolve (unless pinned) and store. Returns (source, status) with status ok|exists|failed."""
    note = ""
    if kind and fetch_url:
        res = Resolution(kind=kind, fetch_url=fetch_url, name=name or "")
    elif kind == "manual":
        res = Resolution(kind="manual", fetch_url=fetch_url or url, name=name or "")
    else:
        with make_client(config) as client:
            res = Resolver(client, HostLimiter(config.scraper.per_host_concurrency)).resolve(url)
        if kind:
            res.kind = kind
        note = res.note
    source = Source(
        name=(name or res.name or url)[:80],
        url=url,
        kind=res.kind,
        category=category or guess_category(url, res.kind),
        fetch_url=res.fetch_url,
        notes=notes or note,
    )
    try:
        db.sources.add(source)
    except DuplicateSourceError as e:
        return e.existing, "exists"
    return source, "ok"


# ── collect ────────────────────────────────────────────────────────────────


def _cmd_collect(args, config: Config, paths: ProjectPaths, sleep=time.sleep) -> int:
    db = Database(paths.db_path)
    collector = Collector(config, paths, db)
    try:
        while True:
            report = collector.run(source_ids=args.source, backfill=args.backfill, force=args.force)
            print(report.summary_line())
            if not args.watch:
                polled = len(report.per_source)
                return 1 if polled and report.sources_fail == polled else 0
            log.info("watch: next run in %ds", args.interval)
            sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        db.close()


# ── sources ────────────────────────────────────────────────────────────────


def _cmd_sources_list(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    rows = []
    for s in db.sources.list():
        st = db.fetch_state.get(s.id)
        rows.append(
            [
                str(s.id),
                "on" if s.enabled else "off",
                s.kind,
                s.category,
                s.name[:40],
                str(db.documents.count(s.id)),
                (st.last_success_at or "-")[:16],
                (st.last_error or "")[:50],
            ]
        )
    print(
        _table(["id", "", "kind", "category", "name", "docs", "last ok", "last error"], rows)
        if rows
        else "no sources yet — try `sources seed` or `sources add <url>`"
    )
    db.close()
    return 0


def _cmd_sources_add(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    try:
        source, status = _add_source(
            db,
            config,
            args.url,
            name=args.name,
            category=args.category,
            kind=args.kind,
            fetch_url=args.fetch_url,
        )
    finally:
        db.close()
    if status == "exists":
        print(f"already exists: #{source.id} {source.name} ({source.fetch_url})")
        return 0
    print(
        f"added #{source.id} [{source.kind}/{source.category}] {source.name} → {source.fetch_url}"
    )
    if source.notes:
        print(f"  note: {source.notes}")
    return 0


def _cmd_sources_toggle(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    ok = db.sources.set_enabled(args.id, args.enable)
    db.close()
    if not ok:
        print(f"no source #{args.id}", file=sys.stderr)
        return 1
    print(f"source #{args.id} {'enabled' if args.enable else 'disabled'}")
    return 0


def _cmd_sources_remove(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    n = db.documents.count(args.id)
    ok = db.sources.remove(args.id)
    db.close()
    if not ok:
        print(f"no source #{args.id}", file=sys.stderr)
        return 1
    print(f"removed source #{args.id} and its {n} documents")
    return 0


def _cmd_sources_resolve(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    source = db.sources.get(args.id)
    if source is None:
        print(f"no source #{args.id}", file=sys.stderr)
        db.close()
        return 1
    with make_client(config) as client:
        res = Resolver(client, HostLimiter(config.scraper.per_host_concurrency)).resolve(source.url)
    changed = (res.kind, res.fetch_url) != (source.kind, source.fetch_url)
    source.kind, source.fetch_url = res.kind, res.fetch_url
    if res.note:
        source.notes = res.note
    if changed:
        with db.transaction():
            db.fetch_state.reset(source.id)
    db.sources.update(source)
    db.close()
    print(
        f"#{source.id} {source.name}: {res.kind} → {res.fetch_url}"
        + (" (changed, cursor reset)" if changed else " (unchanged)")
    )
    return 0


def _cmd_sources_seed(args, config: Config, paths: ProjectPaths) -> int:
    path = args.file or paths.sources_path
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        print(f"cannot read seed file {path}: {e}", file=sys.stderr)
        return 1
    db = Database(paths.db_path)
    counts = {"ok": 0, "exists": 0, "failed": 0}
    for item in doc.get("sources", []):
        url = item.get("url", "")
        if not url:
            continue
        try:
            source, status = _add_source(
                db,
                config,
                url,
                name=item.get("name"),
                category=item.get("category"),
                kind=item.get("kind"),
                fetch_url=item.get("fetch_url"),
                notes=item.get("notes", ""),
            )
        except Exception as e:  # noqa: BLE001 — keep seeding the rest
            log.error("seed %s failed: %s", url, e)
            counts["failed"] += 1
            continue
        counts[status] += 1
        mark = {"ok": "+", "exists": "="}[status]
        print(
            f"{mark} #{source.id} [{source.kind}/{source.category}] {source.name} → {source.fetch_url}"
        )
    for item in doc.get("excluded", []):
        print(f"- skipped {item.get('name', '?')}: {item.get('reason', '')}")
    db.close()
    print(
        f"seed: {counts['ok']} added, {counts['exists']} already present, {counts['failed']} failed"
    )
    return 0


# ── resolve / discover / import / docs ─────────────────────────────────────


def _cmd_resolve(args, config: Config, paths: ProjectPaths) -> int:
    with make_client(config) as client:
        res = Resolver(client, HostLimiter(config.scraper.per_host_concurrency)).resolve(args.url)
    print(f"kind:      {res.kind}\nfetch_url: {res.fetch_url}\nname:      {res.name or '-'}")
    if res.note:
        print(f"note:      {res.note}")
    return 0


def _cmd_discover(args, config: Config, paths: ProjectPaths) -> int:
    from .sources.scraper_llm import TavilyError, TavilySearch

    api_key = load_env_secret(config.tavily.api_key_env, paths.env_path)
    domains = [d.strip() for d in (args.domains or "").split(",") if d.strip()]
    try:
        results = TavilySearch(api_key, config.tavily).search(
            args.query,
            max_results=args.max,
            include_domains=domains or None,
            topic="news" if args.news else "general",
        )
    except TavilyError as e:
        print(str(e), file=sys.stderr)
        return 2
    if not results:
        print("no results")
        return 0
    rows = [[f"{c.score:.2f}", c.title[:60], c.url[:80]] for c in results]
    print(_table(["score", "title", "url"], rows))
    if not args.add:
        return 0
    db = Database(paths.db_path)
    for base in dict.fromkeys(get_base_url(c.url) for c in results):
        source, status = _add_source(db, config, base)
        print(
            f"{'+' if status == 'ok' else '='} #{source.id} [{source.kind}] {source.name} → {source.fetch_url}"
        )
    db.close()
    return 0


def _cmd_import_url(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    try:
        doc_id, created = Collector(config, paths, db).import_url(args.url)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        db.close()
    print(f"{'imported as' if created else 'already stored as'} document #{doc_id}")
    return 0


def _cmd_docs(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    rows = [
        [
            str(r["id"]),
            (r["published_at"] or "-")[:16],
            r["source_name"][:24],
            (r["title"] or "")[:70],
            str(r["text_len"]),
            r["url"][:70],
        ]
        for r in db.documents.list(source_id=args.source, limit=args.limit)
    ]
    total = db.documents.count(args.source)
    db.close()
    print(
        _table(["id", "published", "source", "title", "chars", "url"], rows)
        if rows
        else "no documents yet — run `collect`"
    )
    print(f"({total} documents total)")
    return 0


# ── parser ─────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src", description="ai-analytics-hub — сбор данных (task 1.1)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect", help="poll enabled sources and store new documents")
    p.add_argument("--source", type=int, action="append", help="only this source id (repeatable)")
    p.add_argument("--backfill", action="store_true", help="walk history (older pages, no window)")
    p.add_argument("--force", action="store_true", help="ignore ETag/cursor and re-read everything")
    p.add_argument("--watch", action="store_true", help="keep polling")
    p.add_argument("--interval", type=int, default=900, help="seconds between polls with --watch")
    p.set_defaults(func=_cmd_collect)

    ps = sub.add_parser("sources", help="manage sources").add_subparsers(
        dest="action", required=True
    )
    ps.add_parser("list", help="show sources and their health").set_defaults(func=_cmd_sources_list)
    p = ps.add_parser("add", help="resolve a URL and start collecting it")
    p.add_argument("url")
    p.add_argument("--name")
    p.add_argument("--category", choices=CATEGORIES)
    p.add_argument("--kind", choices=KINDS, help="skip resolution and force this adapter")
    p.add_argument("--fetch-url", help="exact feed/sitemap/page URL to poll (with --kind)")
    p.set_defaults(func=_cmd_sources_add)
    for action, enable in (("enable", True), ("disable", False)):
        p = ps.add_parser(action, help=f"{action} a source")
        p.add_argument("id", type=int)
        p.set_defaults(func=_cmd_sources_toggle, enable=enable)
    p = ps.add_parser("remove", help="delete a source and everything collected from it")
    p.add_argument("id", type=int)
    p.set_defaults(func=_cmd_sources_remove)
    p = ps.add_parser("resolve", help="re-run the resolver for a stored source")
    p.add_argument("id", type=int)
    p.set_defaults(func=_cmd_sources_resolve)
    p = ps.add_parser("seed", help="load sources.json")
    p.add_argument("file", nargs="?")
    p.set_defaults(func=_cmd_sources_seed)

    p = sub.add_parser("resolve", help="dry-run: how would this URL be polled?")
    p.add_argument("url")
    p.set_defaults(func=_cmd_resolve)

    p = sub.add_parser("discover", help="search the web (Tavily) for candidate sources")
    p.add_argument("query")
    p.add_argument("--domains", help="comma-separated include_domains, e.g. gov.ru,cbr.ru")
    p.add_argument("--max", type=int, default=None)
    p.add_argument("--news", action="store_true", help="use Tavily's news topic")
    p.add_argument("--add", action="store_true", help="add each result's site as a source")
    p.set_defaults(func=_cmd_discover)

    p = sub.add_parser("import-url", help="one-off: fetch a page into the manual source")
    p.add_argument("url")
    p.set_defaults(func=_cmd_import_url)

    p = sub.add_parser("docs", help="show collected documents")
    p.add_argument("--source", type=int)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=_cmd_docs)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = Config.load(DEFAULT_PATHS.config_path)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    return args.func(args, config, DEFAULT_PATHS)


if __name__ == "__main__":
    sys.exit(main())
