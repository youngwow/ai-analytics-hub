"""argparse CLI — every command is `_cmd_x(args, config, paths) -> exit code`."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from urllib.parse import urlsplit

from courlan import get_base_url

from .config import Config, ConfigError
from .exceptions import ItemError, QueryError, SourceError
from .models import (
    CATEGORIES,
    EDIT_REASONS,
    ITEM_TYPES,
    KINDS,
    NPA_STATUSES,
    PRIORITIES,
    Resolution,
    Source,
)
from .models.queries import DocumentQuery, FeedQuery
from .paths import DEFAULT_PATHS, ProjectPaths
from .processing import profile as company_profile
from .processing.llm import LlmConfigError, build_provider
from .processing.quality import GOLD_PATH, evaluate, load_gold
from .repositories import Database, DuplicateSourceError
from .services.feed_service import FeedService
from .services.item_service import EDITABLE_FIELDS, REVERTIBLE_FIELDS, ItemService
from .services.processing_service import ProcessingService
from .services.source_service import SourceService
from .sources.base import HostLimiter, make_client
from .sources.collector import Collector
from .sources.resolver import Resolver
from .sources.scraper_search import SEARCH_MAX_RESULTS, SUMMARY_PREFIX, SearchQuery
from .sources.telegram_mtproto import (
    MtprotoError,
    credentials_from_env,
    delete_session,
    login,
    session_status,
)
from .utils import get_logger, load_env_secret

log = get_logger("cli")

_REGULATOR_HOSTS = (
    "gov.ru",
    "government.ru",
    "cbr.ru",
    "duma.gov.ru",
    "pravo.gov.ru",
    "consultant.ru",
    "garant.ru",
    "fstec.ru",
    "fsb.ru",
    "rfrit.ru",
    "xn--h1ahbkg.xn--p1ai",  # рфрит.рф
)
# `--domains @media` / `@regulator` / `@all` → hosts of the enabled sources of that category.
_DOMAIN_PRESETS = {
    "@media": ("media",),
    "@regulator": ("regulator",),
    "@all": ("media", "regulator"),
}
_SEARCH_CATEGORIES = ("media", "regulator")


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
    if kind == "search":
        return "media"  # `search --category regulator` overrides
    host = (urlsplit(url).hostname or "").lower()
    return (
        "regulator"
        if any(host == h or host.endswith("." + h) for h in _REGULATOR_HOSTS)
        else "media"
    )


def _expand_domains(db: Database, spec: str | None) -> list[str]:
    """`a.ru,b.ru` as given; `@media` / `@regulator` / `@all` → hosts of enabled sources."""
    out: list[str] = []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        categories = _DOMAIN_PRESETS.get(item.lower())
        if categories is None:
            if item.startswith("@"):
                raise ValueError(
                    f"unknown domain preset {item!r}; use {', '.join(_DOMAIN_PRESETS)}"
                )
            out.append(item.lower().removeprefix("www."))
            continue
        for s in db.sources.list(enabled_only=True):
            if s.category not in categories or s.kind in ("telegram", "manual", "search"):
                continue
            host = (urlsplit(s.url).hostname or "").lower().removeprefix("www.")
            if host:
                out.append(host)
    return list(dict.fromkeys(out))


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
            # Разовый `collect` опрашивает всё включённое; `--watch` тикает часто и
            # берёт только тех, чья очередь пришла по `next_run_at` (scheduler.py).
            due_only = bool(args.watch and not args.source and not args.backfill and not args.force)
            report = collector.run(
                source_ids=args.source,
                backfill=args.backfill,
                force=args.force,
                due_only=due_only,
            )
            if report.per_source or not due_only:
                print(report.summary_line())
            if not args.watch:
                polled = len(report.per_source)
                return 1 if polled and report.sources_fail == polled else 0
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
                "on" if s.active else s.status[:3],
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
    ok = db.sources.set_status(args.id, "active" if args.enable else "paused")
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
        if status == "ok" and item.get("enabled") is False:
            # Seeded but not polled until `sources enable` (low-priority or paid sources).
            db.sources.set_status(source.id, "paused")
            source.status = "paused"
        mark = {"ok": "+", "exists": "="}[status]
        print(
            f"{mark} #{source.id} [{source.kind}/{source.category}] {source.name} → {source.fetch_url}"
            + ("" if source.active else "  (off)")
        )
    for item in doc.get("excluded", []):
        print(f"- skipped {item.get('name', '?')}: {item.get('reason', '')}")
    db.close()
    print(
        f"seed: {counts['ok']} added, {counts['exists']} already present, {counts['failed']} failed"
    )
    return 0


# ── resolve / discover / search / import / docs ────────────────────────────


def _cmd_resolve(args, config: Config, paths: ProjectPaths) -> int:
    with make_client(config) as client:
        res = Resolver(client, HostLimiter(config.scraper.per_host_concurrency)).resolve(args.url)
    print(f"kind:      {res.kind}\nfetch_url: {res.fetch_url}\nname:      {res.name or '-'}")
    if res.note:
        print(f"note:      {res.note}")
    return 0


def _cmd_discover(args, config: Config, paths: ProjectPaths) -> int:
    """Find candidate *sources* for a query; `--add` runs each hit's site through the resolver."""
    from .sources.scraper_llm import TavilyError, TavilySearch

    api_key = load_env_secret(config.tavily.api_key_env, paths.env_path)
    db = Database(paths.db_path)
    try:
        try:
            domains = _expand_domains(db, args.domains)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        try:
            with make_client(config) as client:
                response = TavilySearch(api_key, config.tavily).search(
                    client,
                    args.query,
                    max_results=args.max,
                    include_domains=domains or None,
                    topic="news" if args.news else "general",
                    country=config.tavily.country or None,
                    language=config.tavily.language or None,
                )
        except TavilyError as e:
            print(str(e), file=sys.stderr)
            return 2
        results = response.results
        if not results:
            print("no results")
            return 0
        rows = [[f"{c.score:.2f}", c.title[:60], c.url[:80]] for c in results]
        print(_table(["score", "title", "url"], rows))
        if not args.add:
            return 0
        for base in dict.fromkeys(get_base_url(c.url) for c in results):
            source, status = _add_source(db, config, base)
            print(
                f"{'+' if status == 'ok' else '='} #{source.id} [{source.kind}] "
                f"{source.name} → {source.fetch_url}"
            )
    finally:
        db.close()
    return 0


def _cmd_search(args, config: Config, paths: ProjectPaths) -> int:
    """Run a Tavily query as a `search` source: the hits and the digest go into `documents`.

    Without `--save` the source stays disabled (an ad-hoc query you can re-run or
    enable later); with it, `collect` keeps polling the query incrementally.
    """
    api_key = load_env_secret(config.tavily.api_key_env, paths.env_path)
    if not api_key:
        # Checked before anything is stored: a query without a key would only leave a dead source row.
        print(
            f"search failed: no Tavily API key: set {config.tavily.api_key_env} "
            "in the environment or .env",
            file=sys.stderr,
        )
        return 2
    db = Database(paths.db_path)
    try:
        try:
            query = SearchQuery(
                query=args.query,
                domains=_expand_domains(db, args.domains),
                days=args.days if args.days is not None else config.tavily.days,
                topic="general" if args.general else "news",
                summary=not args.no_summary,
            )
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        url = query.to_url()
        source, status = _add_source(
            db,
            config,
            url,
            name=args.name or query.query,
            category=args.category or "media",
            kind="search",
            fetch_url=url,
            notes=f"Tavily: {query.describe()}",
        )
        if status == "ok" and not args.save:
            db.sources.set_status(source.id, "paused")
            source.status = "paused"
        elif args.save and not source.active:
            db.sources.set_status(source.id, "active")
            source.status = "active"

        collector = Collector(config, paths, db, tavily_key=api_key)
        result, entry = collector.collect_one(source, force=True)
        if entry["status"] != "ok":
            print(f"search failed: {entry.get('error') or result.error}", file=sys.stderr)
            return 2
        new_ids = set(entry.get("new_external_ids", []))
        hits = [d for d in result.documents if not d.external_id.startswith(SUMMARY_PREFIX)]
        digest = next(
            (d for d in result.documents if d.external_id.startswith(SUMMARY_PREFIX)), None
        )
        limit = max(1, min(args.max or SEARCH_MAX_RESULTS, SEARCH_MAX_RESULTS))
        rows = [
            [
                "+" if d.external_id in new_ids else "=",
                (d.published_at or "-")[:10],
                d.title[:60],
                d.url[:80],
            ]
            for d in hits[:limit]
        ]
        print(_table(["", "published", "title", "url"], rows) if rows else "no results")
        if digest is not None:
            print(f"\n{digest.title}\n{digest.text}\n")
        print(
            f"{len(hits)} hits, {entry['new']} new document(s) → source #{source.id} "
            f"«{source.name}» [{'on' if source.active else 'off'}]"
        )
        if not source.active:
            print(f"  hint: `sources enable {source.id}` keeps polling this query with `collect`")
    finally:
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


# ── telegram (MTProto) ─────────────────────────────────────────────────────


def _telegram_credentials(config: Config, paths: ProjectPaths):
    """Credentials or None, with the reason printed for the user."""
    creds = credentials_from_env(config.telegram, paths)
    if creds is None:
        tg = config.telegram
        print(
            f"no Telegram credentials: set {tg.api_id_env} and {tg.api_hash_env} "
            f"in the environment or .env (get them at https://my.telegram.org)",
            file=sys.stderr,
        )
    return creds


def _cmd_telegram_login(
    args,
    config: Config,
    paths: ProjectPaths,
    prompt=input,
    secret_prompt=getpass.getpass,
) -> int:
    creds = _telegram_credentials(config, paths)
    if creds is None:
        return 1
    try:
        account = login(
            creds, phone=args.phone or "", prompt=prompt, secret_prompt=secret_prompt
        )
    except MtprotoError as e:
        print(f"login failed: {e}", file=sys.stderr)
        return 2
    print(f"signed in as {account}")
    print(f"session: {creds.session_path} — храните как пароль, это доступ к аккаунту")
    return 0


def _cmd_telegram_status(args, config: Config, paths: ProjectPaths) -> int:
    creds = _telegram_credentials(config, paths)
    if creds is None:
        return 1
    print(f"mode:    telegram.mtproto = {config.telegram.mtproto}")
    print(f"api id:  {creds.api_id} (from {config.telegram.api_id_env})")
    try:
        status = session_status(creds)
    except MtprotoError as e:
        print(f"session check failed: {e}", file=sys.stderr)
        return 2
    print(f"session: {status['session_path']}{'' if status['session_exists'] else ' (missing)'}")
    if status["authorized"]:
        print(f"account: {status['account']}")
        return 0
    print("not signed in — run `python -m src telegram login`")
    return 0


def _cmd_telegram_logout(args, config: Config, paths: ProjectPaths) -> int:
    creds = _telegram_credentials(config, paths)
    if creds is None:
        return 1
    if not os.path.exists(creds.session_path):
        print(f"no session file at {creds.session_path}")
        return 0
    if not args.yes:
        print(
            f"this deletes {creds.session_path}; re-run with --yes to confirm",
            file=sys.stderr,
        )
        return 1
    for path in delete_session(creds):
        print(f"removed {path}")
    return 0


def _cmd_docs_unprocessed(args, config: Config, paths: ProjectPaths) -> int:
    """Собрано, но карточки ещё нет — материал доступен, пока обработка догоняет."""
    db, feed = _feed_service(config, paths)
    try:
        result = feed.documents(
            DocumentQuery.build(
                source_ids=[args.source] if args.source else [],
                limit=args.limit,
                timezone_name=config.api.timezone,
            )
        )
    finally:
        db.close()
    rows = [
        [
            str(r["id"]),
            (r["published_at"] or "-")[:10],
            (r["source_name"] or "")[:22],
            (r["title"] or "")[:56],
            str(r["chars"] or 0),
        ]
        for r in result["documents"]
    ]
    print(_table(["id", "date", "источник", "title", "chars"], rows) if rows else "всё обработано")
    print(f"({result['total']} документов без карточки)")
    return 0


def _cmd_docs(args, config: Config, paths: ProjectPaths) -> int:
    if args.unprocessed:
        return _cmd_docs_unprocessed(args, config, paths)
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



# ── processing (task 1.2) ──────────────────────────────────────────────────


def _processing(config: Config, paths: ProjectPaths) -> tuple[Database, ProcessingService]:
    """Database plus service; without a key the provider is None and cards degrade."""
    db = Database(paths.db_path)
    key = load_env_secret(config.llm.api_key_env, paths.env_path)
    provider = build_provider(config.llm, key) if key else None
    if provider is None:
        log.warning("нет %s — обработка пойдёт без модели", config.llm.api_key_env)
    return db, ProcessingService(config, db, provider=provider, embedder=provider)


def _cmd_process(args, config: Config, paths: ProjectPaths) -> int:
    db, service = _processing(config, paths)
    try:
        report = service.run(
            limit=args.limit,
            source_id=args.source,
            since=args.since,
            profile_id=args.profile,
            force=args.force,
            dry_run=args.dry_run,
        )
    except LlmConfigError as e:
        log.error("%s", e)
        return 2
    except ValueError as e:
        log.error("%s", e)
        return 1
    finally:
        service.close()
        db.close()

    if args.dry_run:
        print(
            f"dry-run: {report.documents} документ(ов) → {report.clusters} кластер(ов), "
            "модель не вызывалась"
        )
        return 0
    print(
        f"обработано {report.documents} документ(ов): +{report.items_new} карточек, "
        f"{report.items_joined} присоединено, {report.items_updated} пересобрано, "
        f"{report.degraded} деградировало, {report.needs_review} на проверку"
    )
    print(
        f"вызовов модели {report.calls}, средняя латентность {report.avg_latency_ms} мс, "
        f"всего {report.elapsed_s:.1f} с"
    )
    if report.failed:
        print(f"не обработано: {report.failed}")
    return 2 if (report.degraded and report.items_new) or report.failed else 0


def _feed_service(config: Config, paths: ProjectPaths):
    db = Database(paths.db_path)
    return db, FeedService(config, db)


def _feed_query(args, config: Config, **overrides):
    """Один и тот же фильтр, что и у API: описан один раз в FeedQuery."""
    kwargs = {
        "q": getattr(args, "q", None),
        "type": getattr(args, "type", None),
        "npa_status": getattr(args, "npa_status", None),
        "priority": getattr(args, "priority", None) or [],
        "tags": getattr(args, "tag", None) or [],
        "source_ids": getattr(args, "source", None) or [],
        "date_from": getattr(args, "date_from", None),
        "date_to": getattr(args, "date_to", None),
        "order": getattr(args, "order", "published"),
        "limit": getattr(args, "limit", None),
        "cursor": getattr(args, "cursor", None),
        "include_hidden": getattr(args, "include_hidden", False),
        "timezone_name": config.api.timezone,
    }
    kwargs.update(overrides)
    return FeedQuery.build(**kwargs)


def _cmd_items(args, config: Config, paths: ProjectPaths) -> int:
    db, feed = _feed_service(config, paths)
    try:
        result = feed.items(_feed_query(args, config))
    except QueryError as e:
        log.error("%s", e.message)
        return 1
    finally:
        db.close()
    rows = [
        [
            str(r["id"]),
            (r["published_at"] or "-")[:10],
            r["type"],
            r["priority"],
            str(r["sources_count"]),
            (r["source_name"] or "—")[:18],
            ("⚠ " if r["flags"]["needs_review"] else "")
            + ("· " if r["visibility"] != "visible" else "")
            + (r["title"] or "")[:52],
        ]
        for r in result["items"]
    ]
    print(
        _table(["id", "date", "type", "priority", "src", "источник", "title"], rows)
        if rows
        else "по этому срезу карточек нет"
    )
    print(f"({result['total']} в срезе, показано {len(rows)}, {result['took_ms']} мс)")
    if result["next_cursor"]:
        print(f"следующая страница: --cursor {result['next_cursor']}")
    return 0


def _cmd_item(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    try:
        payload = ItemService(config, db).get_item(args.id)
    except ItemError as e:
        db.close()
        return _report_service_error(e)
    item = payload["item"]
    print(f"#{item.id} [{item.type}/{item.priority}] {item.title}")
    print(f"дата: {item.published_at or '-'} | уверенность: {item.confidence:.2f}", end="")
    print(f" | релевантность: {item.relevance_score:.2f} | модель: {item.model_name or '-'}")
    if item.npa_status or item.npa_key:
        print(f"НПА: статус {item.npa_status or '-'}, идентификатор {item.npa_key or '-'}")
    flags = [
        name
        for name, on in (
            ("деградировано", item.degraded),
            ("нужна проверка", item.needs_review),
            ("дата оценена", item.date_estimated),
        )
        if on
    ]
    if flags:
        print("флаги: " + ", ".join(flags))
    print()
    for line in item.summary.split("\n"):
        print(f"  {line}")
    print()
    if item.reasoning:
        print(f"почему такой приоритет: {item.reasoning}")
    if item.tags:
        print(f"теги: {', '.join(item.tags)}")
    entities = {e.role: e.value for e in payload["entities"]}
    if entities:
        print("сущности: " + "; ".join(f"{k}={v}" for k, v in entities.items()))
    print("\nисточники:")
    for src in payload["sources"]:
        mark = "*" if src["is_canonical"] else " "
        print(f" {mark} {src['source_name'][:24]:24} {src['url']}")
    if payload["events"]:
        print("\nхронология НПА:")
        for event in payload["events"]:
            print(f"  {(event.occurred_at or event.created_at)[:10]} {event.status} ({event.created_by})")
    if payload["revisions"]:
        print("\nправки:")
        for rev in payload["revisions"]:
            print(f"  {rev.created_at[:16]} {rev.actor}: {rev.field}: {rev.old_value} → {rev.new_value}")
    if item.analyst_note:
        print(f"\nзаметка аналитика: {item.analyst_note}")
    db.close()
    return 0


def _cmd_item_edit(args, config: Config, paths: ProjectPaths) -> int:
    fields = {
        "title": args.title,
        "summary": args.summary,
        "priority": args.priority,
        "type": args.type,
        "npa_status": args.npa_status,
        "analyst_note": args.note,
        "tags": [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None,
    }
    db = Database(paths.db_path)
    service = ItemService(config, db)
    try:
        item = service.edit_item(args.id, fields, reason=args.reason or "")
    except ItemError as e:
        return _report_service_error(e)
    finally:
        db.close()
    print(f"#{item.id}: правки сохранены, поля защищены от перезаписи: {item.manual_overrides or '—'}")
    return 0


def _cmd_reprocess(args, config: Config, paths: ProjectPaths) -> int:
    db, service = _processing(config, paths)
    stages = [s.strip() for s in args.stages.split(",") if s.strip()] if args.stages else None
    try:
        item = service.reprocess(
            args.id, stages=stages, keep_human_edits=not args.drop_human_edits
        )
    except LlmConfigError as e:
        log.error("%s", e)
        return 2
    except (ItemError, ValueError) as e:
        log.error("%s", e)
        return 1
    finally:
        service.close()
        db.close()
    print(f"#{item.id}: пересобрано ({item.priority}, {item.type}); сохранено: {item.manual_overrides or '—'}")
    return 2 if item.degraded else 0


def _cmd_npa_event(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    service = ItemService(config, db)
    try:
        service.add_npa_event(
            args.id,
            args.status,
            occurred_at=args.occurred_at,
            source_url=args.source_url or "",
            note=args.note or "",
        )
    except ItemError as e:
        return _report_service_error(e)
    finally:
        db.close()
    print(f"#{args.id}: событие «{args.status}» добавлено")
    return 0


def _cmd_profile(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    try:
        if args.action == "list":
            rows = [
                [str(p.id), p.name[:40], f"v{p.version}", "по умолчанию" if p.is_default else ""]
                for p in db.profiles.list()
            ] or [["—", "профилей нет", "", ""]]
            print(_table(["id", "название", "версия", ""], rows))
            return 0
        if args.action == "use":
            if not db.profiles.set_default(args.id):
                log.error("профиль #%s не найден", args.id)
                return 1
            print(f"профиль #{args.id} стал профилем по умолчанию")
            return 0
        if args.action == "set":
            try:
                with open(args.file, encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError) as e:
                log.error("не прочитать %s: %s", args.file, e)
                return 1
            name = payload.pop("name", None) or args.name
            if not name:
                log.error("в файле нет поля name и не передан --name")
                return 1
            saved = db.profiles.save(company_profile.CompanyProfile(name=name, payload=payload))
            print(f"профиль «{saved.name}» сохранён как версия {saved.version}")
            return 0
        profile = company_profile.ensure_default(db)
        print(f"#{profile.id} {profile.name} (версия {profile.version})")
        print(profile.prompt_block())
        return 0
    finally:
        db.close()


def _cmd_quality(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    service = ProcessingService(config, db)
    summary = service.quality_summary(since=args.since, until=args.until)
    print(
        f"карточек {summary['items']} "
        f"(high {summary['by_priority'].get('high', 0)}, "
        f"medium {summary['by_priority'].get('medium', 0)}, "
        f"low {summary['by_priority'].get('low', 0)})"
    )
    print(
        f"деградировало {summary['degraded']}, на проверку {summary['hallucination_flags']}, "
        f"доля правок {summary['edited_share']}"
    )
    print(
        f"вызовов модели {summary['calls']} (ошибок {summary['failed_calls']}), "
        f"средняя латентность {summary['avg_latency_ms']} мс, "
        f"токенов {summary['tokens_in']}→{summary['tokens_out']}"
    )
    code = 0
    if args.gold:
        rows = load_gold(args.gold_path or GOLD_PATH)
        if not rows:
            log.error("золотой набор не найден: %s", args.gold_path or GOLD_PATH)
            code = 1
        else:
            result = evaluate(db, rows)
            data = result.as_dict()
            print(
                f"\nзолотой набор: {data['scored']} размечено, {data['skipped']} пропущено "
                f"(вложения), {data['missing']} без карточки"
            )
            print(
                f"recall по high {data['high_recall']} (цель ≥ 0.95), "
                f"точность приоритета {data['priority_accuracy']} (цель ≥ 0.80), "
                f"точность типа {data['type_accuracy']}"
            )
            print("приёмка пройдена" if data["passed"] else "приёмка НЕ пройдена")
            code = 0 if data["passed"] else 1
    db.close()
    return code



# ── управление источниками и данными (task 1.4) ────────────────────────────


def _sources_service(config: Config, paths: ProjectPaths):
    db = Database(paths.db_path)
    return db, SourceService(config, db)


def _report_service_error(e) -> int:
    log.error("%s", e.message)
    if e.details:
        print(f"  {e.details}")
    return 2 if e.code in ("network_unreachable", "telegram_preview_unavailable") else 1


def _cmd_sources_probe(args, config: Config, paths: ProjectPaths) -> int:
    db, svc = _sources_service(config, paths)
    try:
        result = svc.probe(args.url)
    except SourceError as e:
        return _report_service_error(e)
    finally:
        db.close()
    print(f"тип:       {result.resolved_type}")
    print(f"адрес:     {result.feed_url or '-'}")
    print(f"название:  {result.title or '-'}")
    print(f"как нашли: {result.detection_method}; периодичность: {result.suggested_poll_interval}")
    if result.already_exists:
        print(f"уже добавлен как источник #{result.already_exists_source_id}")
    if result.warnings:
        print(f"предупреждения: {', '.join(result.warnings)}")
    if result.preview:
        print("\nпоследние материалы:")
        for row in result.preview:
            print(f"  {(row['published_at'] or '-')[:16]}  {(row['title'] or '')[:70]}")
    else:
        print("превью пустое")
    return 0 if result.resolved_type != "unsupported" else 1


def _cmd_sources_health(args, config: Config, paths: ProjectPaths) -> int:
    db, svc = _sources_service(config, paths)
    try:
        data = svc.health(args.id, args.limit)
    except SourceError as e:
        return _report_service_error(e)
    finally:
        db.close()
    source = data["source"]
    print(f"#{source.id} {source.name} [{source.status}] каждые {source.poll_interval}")
    print(f"документов {data['documents']}, следующий опрос {source.next_run_at or '-'}")
    print(f"последний успех {data['last_success_at'] or '-'}, подряд неудач {data['consecutive_failures']}")
    if data["last_error"]:
        print(f"последняя ошибка: {data['last_error'][:120]}")
    rows = [
        [
            r.started_at[:16],
            str(r.items_found),
            str(r.items_new),
            r.error_code or "ok",
            (r.error_message or "")[:48],
        ]
        for r in data["runs"]
    ]
    print()
    print(_table(["когда", "нашли", "новых", "код", "ошибка"], rows) if rows else "опросов ещё не было")
    return 0


def _cmd_sources_status(args, config: Config, paths: ProjectPaths) -> int:
    """pause / resume / restore — одна команда, разные целевые состояния."""
    db, svc = _sources_service(config, paths)
    try:
        source = svc.restore(args.id) if args.action == "restore" else svc.update(
            args.id, status="paused" if args.action == "pause" else "active"
        )
    except SourceError as e:
        return _report_service_error(e)
    finally:
        db.close()
    print(f"#{source.id} «{source.name}» → {source.status}")
    return 0


def _cmd_sources_refresh(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    svc = SourceService(config, db)
    try:
        source = svc.get(args.id)
    except SourceError as e:
        db.close()
        return _report_service_error(e)
    tavily_key = load_env_secret(config.tavily.api_key_env, paths.env_path)
    try:
        run = svc.refresh(
            args.id, Collector(config, paths, db, tavily_key=tavily_key or None), force=args.force
        )
    except SourceError as e:
        return _report_service_error(e)
    finally:
        db.close()
    if run.error_code:
        print(f"#{source.id} «{source.name}»: {run.error_message or run.error_code}")
        return 2
    print(f"#{source.id} «{source.name}»: найдено {run.items_found}, новых {run.items_new}")
    return 0


def _item_service(config: Config, paths: ProjectPaths):
    """Карточные операции без модели: провайдер здесь не нужен."""
    db = Database(paths.db_path)
    return db, ItemService(config, db)


def _cmd_items_hide(args, config: Config, paths: ProjectPaths) -> int:
    db, svc = _item_service(config, paths)
    try:
        if len(args.id) > 1:
            changed = svc.bulk_visibility(args.id, args.scope, args.reason or "")
            print(f"скрыто карточек: {changed} (область: {args.scope})")
        else:
            item = svc.set_visibility(args.id[0], args.scope, args.reason or "")
            print(f"#{item.id}: {item.visibility}")
    except ItemError as e:
        return _report_service_error(e)
    finally:
        db.close()
    return 0


def _cmd_items_unhide(args, config: Config, paths: ProjectPaths) -> int:
    db, svc = _item_service(config, paths)
    try:
        if len(args.id) > 1:
            print(f"возвращено в ленту: {svc.bulk_visibility(args.id, 'visible')}")
        else:
            item = svc.set_visibility(args.id[0], restore=True)
            print(f"#{item.id}: {item.visibility}")
    except ItemError as e:
        return _report_service_error(e)
    finally:
        db.close()
    return 0


def _cmd_item_note(args, config: Config, paths: ProjectPaths) -> int:
    db, svc = _item_service(config, paths)
    try:
        note = svc.add_note(args.id, args.text, args.author or "")
    except ItemError as e:
        return _report_service_error(e)
    finally:
        db.close()
    print(f"#{note.item_id}: заметка сохранена ({len(note.body)} символов)")
    return 0


def _cmd_item_revert(args, config: Config, paths: ProjectPaths) -> int:
    db, svc = _item_service(config, paths)
    try:
        item = svc.revert(args.id, args.field)
    except ItemError as e:
        return _report_service_error(e)
    finally:
        db.close()
    print(f"#{item.id}: поле «{args.field}» возвращено к версии модели")
    print(f"под защитой человека осталось: {item.manual_overrides or '—'}")
    return 0


def _cmd_item_revisions(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    try:
        revisions = ItemService(config, db).revisions(args.id)
    except ItemError as e:
        return _report_service_error(e)
    finally:
        db.close()
    rows = [
        [
            r.created_at[:16],
            "модель" if r.source_of_change == "llm" else r.actor,
            r.field,
            (r.old_value or "—")[:34],
            (r.new_value or "—")[:34],
            r.edit_reason or "",
        ]
        for r in revisions
    ]
    print(
        _table(["когда", "кто", "поле", "было", "стало", "причина"], rows)
        if rows
        else "правок не было"
    )
    return 0


def _cmd_items_add(args, config: Config, paths: ProjectPaths) -> int:
    db, svc = _processing(config, paths)
    try:
        result = svc.add_manual(
            title=args.title or "",
            url=args.url or "",
            text=args.text or "",
            published_at=args.published_at,
            item_type=args.type,
            npa_status=args.npa_status,
            run_llm=not args.no_llm,
            force=args.force,
        )
    except ItemError as e:
        if e.code == "possible_duplicate":
            log.error("%s", e.message)
            print(f"  найдено: карточка #{e.details.get('item_id')} ({e.details.get('reason')})")
            print("  создать всё равно: повторите с --force")
            return 1
        return _report_service_error(e)
    finally:
        svc.close()
        db.close()
    print(f"карточка #{result['item_id']} создана (документ #{result['document_id']}, origin=manual)")
    return 0


def _cmd_serve(args, config: Config, paths: ProjectPaths) -> int:
    """HTTP-API через фабрику `src.main:create_app`; адрес и порт — из `Settings`."""
    import uvicorn

    from .config import get_settings

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port
    print(f"API на http://{host}:{port}" + (" (документация /docs)" if settings.docs else ""))
    uvicorn.run(
        "src.main:create_app",
        factory=True,
        host=host,
        port=port,
        log_level="info",
        reload=bool(args.reload),
    )
    return 0



def _cmd_digest(args, config: Config, paths: ProjectPaths) -> int:
    db, feed = _feed_service(config, paths)
    try:
        result = feed.digest(
            _feed_query(args, config, limit=200, order="priority"),
            fmt=args.format,
            title=args.title or "",
            include_notes=args.include_notes,
        )
    except QueryError as e:
        log.error("%s", e.message)
        return 1
    finally:
        db.close()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(result["body"])
        print(f"{result['items']} материал(ов) → {args.out}")
    else:
        print(result["body"])
    return 0


def _cmd_status(args, config: Config, paths: ProjectPaths) -> int:
    db, feed = _feed_service(config, paths)
    try:
        data = feed.status()
    finally:
        db.close()
    print(f"последний сбор: {data['last_collect_at'] or '—'}")
    print(
        f"документов {data['documents']}, карточек {data['items']}, "
        f"без карточки {data['unprocessed']}"
    )
    print("источники: " + ", ".join(f"{k} {v}" for k, v in sorted(data["sources"].items())))
    if data["stale_sources"]:
        rows = [
            [
                str(s["id"]),
                s["name"][:28],
                f"{s['overdue_minutes']} мин",
                str(s["consecutive_failures"]),
                (s["last_error"] or "")[:40],
            ]
            for s in data["stale_sources"]
        ]
        print()
        print(_table(["id", "источник", "просрочен", "неудач", "ошибка"], rows))
    return 0


# ── parser ─────────────────────────────────────────────────────────────────


def _feed_filter_args(parser) -> None:
    """Фильтры ленты — одни и те же у `items` и `digest`."""
    parser.add_argument("--q", help="search over card, tags, entities and the original text")
    parser.add_argument("--type", choices=ITEM_TYPES)
    parser.add_argument("--npa-status", dest="npa_status", choices=NPA_STATUSES)
    parser.add_argument("--priority", choices=PRIORITIES, action="append")
    parser.add_argument("--tag", action="append")
    parser.add_argument("--source", type=int, action="append", help="source id (repeatable)")
    parser.add_argument("--from", dest="date_from", help="local date or ISO timestamp")
    parser.add_argument("--to", dest="date_to")
    parser.add_argument("--order", choices=("published", "priority", "processed"),
                        default="published")


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
    p.add_argument(
        "--interval",
        type=int,
        default=60,
        help="seconds between watch ticks; each tick polls only sources due by their schedule",
    )
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

    p = ps.add_parser("probe", help="resolve a URL and preview it without saving")
    p.add_argument("url")
    p.set_defaults(func=_cmd_sources_probe)
    p = ps.add_parser("health", help="poll history and the last error for a source")
    p.add_argument("id", type=int)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=_cmd_sources_health)
    for action in ("pause", "resume", "restore"):
        p = ps.add_parser(action, help=f"{action} a source")
        p.add_argument("id", type=int)
        p.set_defaults(func=_cmd_sources_status, action=action)
    p = ps.add_parser("refresh", help="poll one source right now")
    p.add_argument("id", type=int)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_cmd_sources_refresh)

    p = sub.add_parser("resolve", help="dry-run: how would this URL be polled?")
    p.add_argument("url")
    p.set_defaults(func=_cmd_resolve)

    p = sub.add_parser("discover", help="search the web (Tavily) for candidate sources")
    p.add_argument("query")
    p.add_argument(
        "--domains",
        help="comma-separated include_domains (gov.ru,cbr.ru) or @media/@regulator/@all",
    )
    p.add_argument("--max", type=int, default=None)
    p.add_argument("--news", action="store_true", help="use Tavily's news topic")
    p.add_argument("--add", action="store_true", help="add each result's site as a source")
    p.set_defaults(func=_cmd_discover)

    p = sub.add_parser(
        "search", help="run a Tavily news query as a source: hits + digest go into documents"
    )
    p.add_argument("query")
    p.add_argument(
        "--domains",
        help="comma-separated include_domains (gov.ru,cbr.ru) or @media/@regulator/@all",
    )
    p.add_argument("--days", type=int, default=None, help="recency window in days (config: 7)")
    p.add_argument("--max", type=int, default=None, help="rows to print (≤ 20)")
    p.add_argument("--category", choices=_SEARCH_CATEGORIES, default=None)
    p.add_argument("--general", action="store_true", help="Tavily 'general' topic instead of news")
    p.add_argument(
        "--no-summary", action="store_true", help="skip Tavily's answer (the «Сводка» document)"
    )
    p.add_argument("--save", action="store_true", help="keep the query enabled for `collect`")
    p.add_argument("--name", help="source name (default: the query itself)")
    p.set_defaults(func=_cmd_search)

    pt = sub.add_parser("telegram", help="MTProto session for Telegram channels").add_subparsers(
        dest="action", required=True
    )
    p = pt.add_parser("login", help="sign in once and store data/<session>.session")
    p.add_argument("--phone", help="phone number in +79991234567 form (asked interactively if absent)")
    p.set_defaults(func=_cmd_telegram_login)
    pt.add_parser("status", help="credentials, session file and whether it is signed in").set_defaults(
        func=_cmd_telegram_status
    )
    p = pt.add_parser("logout", help="delete the stored session")
    p.add_argument("--yes", action="store_true", help="confirm deletion")
    p.set_defaults(func=_cmd_telegram_logout)

    p = sub.add_parser("process", help="turn collected documents into feed cards (LLM)")
    p.add_argument("--limit", type=int, default=None, help="documents per run (config: 200)")
    p.add_argument("--source", type=int, help="only documents from this source id")
    p.add_argument("--since", help="only documents published after this ISO date")
    p.add_argument("--profile", type=int, help="company profile id (default: the default one)")
    p.add_argument("--force", action="store_true", help="re-read documents that already have cards")
    p.add_argument("--dry-run", action="store_true", help="plan only: no model calls, no writes")
    p.set_defaults(func=_cmd_process)

    p = sub.add_parser("items", help="the feed: cards with filters")
    _feed_filter_args(p)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--cursor", help="next page token from the previous call")
    p.add_argument(
        "--include-hidden", action="store_true", help="show hidden and deleted cards too"
    )
    p.set_defaults(func=_cmd_items)

    p = sub.add_parser("digest", help="export the current slice for a manager")
    _feed_filter_args(p)
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument("--title")
    p.add_argument("--include-notes", action="store_true", help="include analyst notes")
    p.add_argument("--out", help="write to a file instead of stdout")
    p.set_defaults(func=_cmd_digest)

    sub.add_parser("status", help="collection and processing health").set_defaults(
        func=_cmd_status
    )

    p = sub.add_parser("item", help="one card: summary, entities, sources, history")
    p.add_argument("id", type=int)
    p.set_defaults(func=_cmd_item)

    p = sub.add_parser("edit", help="analyst edit; edited fields survive reprocessing")
    p.add_argument("id", type=int)
    p.add_argument("--title")
    p.add_argument("--summary")
    p.add_argument("--priority", choices=PRIORITIES)
    p.add_argument("--type", choices=ITEM_TYPES)
    p.add_argument("--npa-status", dest="npa_status")
    p.add_argument("--tags", help="comma-separated list")
    p.add_argument("--note", help="analyst note — never sent to the model")
    p.add_argument(
        "--reason", choices=EDIT_REASONS, help="why the edit was needed — feeds quality metrics"
    )
    p.set_defaults(func=_cmd_item_edit)

    p = sub.add_parser("reprocess", help="re-run the model for one card")
    p.add_argument("id", type=int)
    p.add_argument("--stages", help=f"comma-separated subset of {','.join(EDITABLE_FIELDS)}")
    p.add_argument(
        "--drop-human-edits", action="store_true", help="let the model overwrite edited fields"
    )
    p.set_defaults(func=_cmd_reprocess)

    p = sub.add_parser("npa-event", help="add a step to a bill's timeline by hand")
    p.add_argument("id", type=int)
    p.add_argument("--status", required=True)
    p.add_argument("--occurred-at", dest="occurred_at")
    p.add_argument("--source-url", dest="source_url")
    p.add_argument("--note")
    p.set_defaults(func=_cmd_npa_event)

    pprofile = sub.add_parser("profile", help="company profile used for prioritisation")
    pprofile.set_defaults(func=_cmd_profile, action="show")
    pp = pprofile.add_subparsers(dest="action")
    pp.add_parser("show", help="the default profile as the model sees it").set_defaults(
        func=_cmd_profile, action="show"
    )
    pp.add_parser("list", help="stored profiles").set_defaults(func=_cmd_profile, action="list")
    p = pp.add_parser("use", help="make a profile the default")
    p.add_argument("id", type=int)
    p.set_defaults(func=_cmd_profile, action="use")
    p = pp.add_parser("set", help="save a profile from a JSON file as a new version")
    p.add_argument("file")
    p.add_argument("--name")
    p.set_defaults(func=_cmd_profile, action="set")

    p = sub.add_parser("quality", help="processing metrics; --gold checks the labelled set")
    p.add_argument("--from", dest="since")
    p.add_argument("--to", dest="until")
    p.add_argument("--gold", action="store_true", help="evaluate against the gold set")
    p.add_argument("--gold-path", help=f"path to the gold set (default: {'tests/fixtures/gold/gold_set.jsonl'})")
    p.set_defaults(func=_cmd_quality)

    p = sub.add_parser("hide", help="hide cards from the feed or from the next digest")
    p.add_argument("id", type=int, nargs="+")
    p.add_argument("--scope", choices=("feed", "digest"), default="feed")
    p.add_argument("--reason")
    p.set_defaults(func=_cmd_items_hide)

    p = sub.add_parser("unhide", help="bring cards back to the feed")
    p.add_argument("id", type=int, nargs="+")
    p.set_defaults(func=_cmd_items_unhide)

    p = sub.add_parser("note", help="analyst note on a card — never sent to the model")
    p.add_argument("id", type=int)
    p.add_argument("--text", required=True)
    p.add_argument("--author")
    p.set_defaults(func=_cmd_item_note)

    p = sub.add_parser("revert", help="restore the model's version of a field")
    p.add_argument("id", type=int)
    p.add_argument("--field", required=True, choices=REVERTIBLE_FIELDS)
    p.set_defaults(func=_cmd_item_revert)

    p = sub.add_parser("revisions", help="edit history of a card")
    p.add_argument("id", type=int)
    p.set_defaults(func=_cmd_item_revisions)

    p = sub.add_parser("add-item", help="add a publication by hand (US-12, US-13)")
    p.add_argument("--url")
    p.add_argument("--title")
    p.add_argument("--text")
    p.add_argument("--published-at", dest="published_at")
    p.add_argument("--type", choices=ITEM_TYPES, default="news")
    p.add_argument("--npa-status", dest="npa_status")
    p.add_argument("--no-llm", action="store_true", help="do not call the model")
    p.add_argument("--force", action="store_true", help="create even if a duplicate is found")
    p.set_defaults(func=_cmd_items_add)

    p = sub.add_parser("serve", help="run the HTTP API")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--reload", action="store_true", help="restart on source changes (dev)")
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("import-url", help="one-off: fetch a page into the manual source")
    p.add_argument("url")
    p.set_defaults(func=_cmd_import_url)

    p = sub.add_parser("docs", help="show collected documents")
    p.add_argument("--source", type=int)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument(
        "--unprocessed", action="store_true", help="only documents that have no card yet"
    )
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
