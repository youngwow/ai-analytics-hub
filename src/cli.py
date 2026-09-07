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

from .common import get_logger, load_env_secret
from .config import Config, ConfigError
from .models import CATEGORIES, DIRECTIONS, ITEM_TYPES, KINDS, PRIORITIES, Resolution, Source
from .paths import DEFAULT_PATHS, ProjectPaths
from .processing import profile as company_profile
from .processing.llm import LlmConfigError, build_embedding_provider, build_llm_provider
from .processing.quality import GOLD_PATH, evaluate, load_gold
from .processing.service import EDITABLE_FIELDS, ProcessingService
from .sources.base import HostLimiter, make_client
from .sources.collector import Collector
from .sources.resolver import Resolver
from .sources.scraper_search import SEARCH_MAX_RESULTS, SearchQuery
from .sources.telegram_mtproto import (
    MtprotoError,
    credentials_from_env,
    delete_session,
    login,
    session_status,
)
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
    direction: str = "both",
    source_class: str = "ordinary",
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
        direction=direction,
        source_class=source_class,
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
                s.direction,
                s.name[:40],
                str(db.documents.count(s.id)),
                st.coverage_status,
                st.backlog_status,
                (st.last_success_at or "-")[:16],
                (st.last_error or "")[:50],
            ]
        )
    print(
        _table(
            ["id", "", "kind", "category", "dir", "name", "docs", "coverage", "backlog", "last ok", "last error"],
            rows,
        )
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
            direction=getattr(args, "direction", "both"),
            source_class=getattr(args, "source_class", "ordinary"),
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
    print(f"decommissioned source #{args.id}; preserved {n} documents")
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
                direction=item.get("direction", "both"),
                source_class=item.get("source_class", "ordinary"),
            )
        except Exception as e:  # noqa: BLE001 — keep seeding the rest
            log.error("seed %s failed: %s", url, e)
            counts["failed"] += 1
            continue
        counts[status] += 1
        if status == "ok" and item.get("enabled") is False:
            # Seeded but not polled until `sources enable` (low-priority or paid sources).
            db.sources.set_enabled(source.id, False)
            source.enabled = False
        mark = {"ok": "+", "exists": "="}[status]
        print(
            f"{mark} #{source.id} [{source.kind}/{source.category}] {source.name} → {source.fetch_url}"
            + ("" if source.enabled else "  (off)")
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
    """Run a Tavily query as a `search` source: source-backed hits go into `documents`.

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
            direction=getattr(args, "direction", "both"),
        )
        if status == "ok" and not args.save:
            db.sources.set_enabled(source.id, False)
            source.enabled = False
        elif args.save and not source.enabled:
            db.sources.set_enabled(source.id, True)
            source.enabled = True

        collector = Collector(config, paths, db, tavily_key=api_key)
        result, entry = collector.collect_one(source, force=True)
        if entry["status"] != "ok":
            print(f"search failed: {entry.get('error') or result.error}", file=sys.stderr)
            return 2
        new_ids = set(entry.get("new_external_ids", []))
        hits = result.documents
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
        print(
            f"{len(hits)} hits, {entry['new']} new document(s) → source #{source.id} "
            f"«{source.name}» [{'on' if source.enabled else 'off'}]"
        )
        if not source.enabled:
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



# ── processing (task 1.2) ──────────────────────────────────────────────────


def _processing(config: Config, paths: ProjectPaths) -> tuple[Database, ProcessingService]:
    """Database plus service; without a key the provider is None and cards degrade."""
    db = Database(paths.db_path)
    key = load_env_secret(config.llm.api_key_env, paths.env_path)
    provider = build_llm_provider(config.llm, key) if key else None
    embedding_key = load_env_secret(config.embeddings.api_key_env, paths.env_path)
    embedder = (
        build_embedding_provider(config.embeddings, embedding_key) if embedding_key else None
    )
    if provider is None:
        log.warning("нет %s — обработка пойдёт без модели", config.llm.api_key_env)
    if embedder is None:
        log.warning("нет %s — embedding-кандидаты недоступны", config.embeddings.api_key_env)
    return db, ProcessingService(config, db, provider=provider, embedder=embedder)


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


def _cmd_items(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    rows = db.items.list(
        type_=args.type,
        priority=args.priority,
        tag=args.tag,
        query=args.q,
        since=args.since,
        limit=args.limit,
    )
    table = [
        [
            str(r["id"]),
            (r["published_at"] or "-")[:10],
            r["type"],
            r["priority"],
            str(r["sources_count"]),
            ("⚠ " if r["needs_review"] else "") + (r["title"] or "")[:70],
        ]
        for r in rows
    ]
    total = db.items.count()
    db.close()
    print(
        _table(["id", "date", "type", "priority", "src", "title"], table)
        if table
        else "карточек нет — запустите `process`"
    )
    print(f"({total} карточек всего)")
    return 0


def _cmd_item(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    payload = ProcessingService(config, db).get_item(args.id)
    if payload is None:
        db.close()
        log.error("карточка #%s не найдена", args.id)
        return 1
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
    service = ProcessingService(config, db)
    try:
        item = service.edit_item(args.id, fields)
    except ValueError as e:
        log.error("%s", e)
        return 1
    finally:
        db.close()
    print(f"#{item.id}: правки сохранены, поля защищены от перезаписи: {item.edited_fields or '—'}")
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
    except ValueError as e:
        log.error("%s", e)
        return 1
    finally:
        service.close()
        db.close()
    print(f"#{item.id}: пересобрано ({item.priority}, {item.type}); сохранено: {item.edited_fields or '—'}")
    return 2 if item.degraded else 0


def _cmd_npa_event(args, config: Config, paths: ProjectPaths) -> int:
    db = Database(paths.db_path)
    service = ProcessingService(config, db)
    try:
        service.add_npa_event(
            args.id,
            args.status,
            occurred_at=args.occurred_at,
            source_url=args.source_url or "",
            note=args.note or "",
        )
    except ValueError as e:
        log.error("%s", e)
        return 1
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
    p.add_argument("--direction", choices=DIRECTIONS, default="both")
    p.add_argument("--source-class", choices=("ordinary", "regulator", "npa"), default="ordinary")
    p.set_defaults(func=_cmd_sources_add)
    for action, enable in (("enable", True), ("disable", False)):
        p = ps.add_parser(action, help=f"{action} a source")
        p.add_argument("id", type=int)
        p.set_defaults(func=_cmd_sources_toggle, enable=enable)
    p = ps.add_parser("remove", help="stop a source while preserving collected evidence")
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
    p.add_argument(
        "--domains",
        help="comma-separated include_domains (gov.ru,cbr.ru) or @media/@regulator/@all",
    )
    p.add_argument("--max", type=int, default=None)
    p.add_argument("--news", action="store_true", help="use Tavily's news topic")
    p.add_argument("--add", action="store_true", help="add each result's site as a source")
    p.set_defaults(func=_cmd_discover)

    p = sub.add_parser(
        "search", help="run a Tavily query as a source: source-backed hits become documents"
    )
    p.add_argument("query")
    p.add_argument(
        "--domains",
        help="comma-separated include_domains (gov.ru,cbr.ru) or @media/@regulator/@all",
    )
    p.add_argument("--days", type=int, default=None, help="recency window in days (config: 7)")
    p.add_argument("--max", type=int, default=None, help="rows to print (≤ 20)")
    p.add_argument("--category", choices=_SEARCH_CATEGORIES, default=None)
    p.add_argument("--direction", choices=DIRECTIONS, default="both")
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
    p.add_argument("--type", choices=ITEM_TYPES)
    p.add_argument("--priority", choices=PRIORITIES)
    p.add_argument("--tag")
    p.add_argument("--q", help="full-text query over title and summary")
    p.add_argument("--since", help="ISO date lower bound")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=_cmd_items)

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
