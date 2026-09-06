"""B1 Collection benchmark.

B1-D replays captured real source responses through the adapter contract.
B1-R probes every configured source surface without changing the product DB.
Reports are JSON and intentionally keep deterministic and live evidence apart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common import load_env_secret  # noqa: E402
from src.config import Config  # noqa: E402
from src.models import FetchState, Source  # noqa: E402
from src.paths import ProjectPaths  # noqa: E402
from src.sources.base import HostLimiter, build_adapters, make_client  # noqa: E402
from src.sources.collector import Collector  # noqa: E402
from src.sources.scraper_llm import TAVILY_SEARCH_URL  # noqa: E402
from src.storage import Database  # noqa: E402

HERE = Path(__file__).resolve().parent
FIXTURES = ROOT / "tests" / "fixtures"
REPORTS = HERE / "reports"
UTC = timezone.utc
FROZEN_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _percent(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 2) if denominator else None


def _headers(fixture: str) -> dict[str, str]:
    if fixture.endswith(".json"):
        return {"content-type": "application/json"}
    if fixture.endswith(".xml"):
        return {"content-type": "application/rss+xml"}
    return {"content-type": "text/html; charset=utf-8"}


class ReplayTransport:
    """One-response transport over captured bytes, including conditional GET and failures."""

    def __init__(self, url: str, body: bytes, headers: dict[str, str]):
        self.url = url
        self.body = body
        self.headers = {**headers, "etag": '"b1-capture"'}
        self.requests: list[dict] = []
        self.mode = "ok"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"method": request.method, "url": str(request.url)})
        if self.mode == "timeout":
            raise httpx.ReadTimeout("B1 injected timeout", request=request)
        if request.headers.get("if-none-match") == '"b1-capture"':
            return httpx.Response(304, request=request)
        if str(request.url) != self.url:
            return httpx.Response(404, content=b"not captured", request=request)
        return httpx.Response(200, content=self.body, headers=self.headers, request=request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class OperationalTransport:
    """Stateful two-source replay for isolation, recovery and idempotency checks."""

    def __init__(self):
        self.recovery_attempts = 0
        self.bodies = {
            "https://example.ru/good.xml": (FIXTURES / "rss_yandex_fulltext.xml").read_bytes(),
            "https://example.org/recover.xml": (FIXTURES / "rss_cbr_fulltext.xml").read_bytes(),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("recover.xml") and self.recovery_attempts < 3:
            self.recovery_attempts += 1
            raise httpx.ReadTimeout("B1 first-run outage", request=request)
        if request.headers.get("if-none-match") == f'"{url}"':
            return httpx.Response(304, request=request)
        body = self.bodies.get(url)
        if body is None:
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/rss+xml", "etag": f'"{url}"'},
            request=request,
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _config(timeout: float = 12.0) -> Config:
    cfg = Config.load(str(ROOT / "config.yaml"))
    return replace(cfg, scraper=replace(cfg.scraper, request_timeout=timeout, fetch_fulltext=False))


def run_deterministic() -> dict:
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    cfg = _config()
    limiter = HostLimiter(cfg.scraper.per_host_concurrency)
    rows = []
    for index, spec in enumerate(manifest["scenarios"], start=1):
        body = (FIXTURES / spec["fixture"]).read_bytes()
        route_url = TAVILY_SEARCH_URL if spec["kind"] == "search" else spec["url"]
        replay = ReplayTransport(route_url, body, _headers(spec["fixture"]))
        adapters = build_adapters(cfg, limiter, tavily_key="b1-replay-key")
        source = Source(
            id=index,
            name=spec["id"],
            url=spec["url"],
            fetch_url=spec["url"],
            kind=spec["kind"],
            category=spec["category"],
        )
        started = time.perf_counter()
        with make_client(cfg, replay.transport()) as client:
            result = adapters[spec["kind"]].fetch(
                source,
                FetchState(source_id=index),
                client,
                now=FROZEN_NOW,
                since=FROZEN_NOW - timedelta(hours=72),
            )
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        docs = result.documents
        ids = {doc.external_id for doc in docs}
        ids_sha256 = hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()
        missing_ids = sorted(set(spec["required_ids"]) - ids)
        field_failures = []
        for doc in docs:
            for field in spec["required_fields"]:
                if not getattr(doc, field, None):
                    field_failures.append({"external_id": doc.external_id, "field": field})
        passed = (
            result.error is None
            and len(docs) == spec["expected_count"]
            and ids_sha256 == spec["expected_ids_sha256"]
            and not missing_ids
            and not field_failures
        )
        rows.append(
            {
                "id": spec["id"],
                "kind": spec["kind"],
                "passed": passed,
                "expected": spec["expected_count"],
                "observed": len(docs),
                "missing_ids": missing_ids,
                "ids_sha256": ids_sha256,
                "expected_ids_sha256": spec["expected_ids_sha256"],
                "field_failures": field_failures,
                "error": result.error,
                "latency_ms": elapsed,
            }
        )

    # Adapter state guardrails are checked separately from content scenarios.
    probe = ReplayTransport(
        "https://example.ru/rss.xml",
        (FIXTURES / "rss_yandex_fulltext.xml").read_bytes(),
        {"content-type": "application/rss+xml"},
    )
    adapter = build_adapters(cfg, HostLimiter(1))["rss"]
    source = Source(id=999, name="state", url=probe.url, fetch_url=probe.url, kind="rss")
    state = FetchState(source_id=999)
    with make_client(cfg, probe.transport()) as client:
        first = adapter.fetch(source, state, client, now=FROZEN_NOW)
        state.etag = first.state_update.get("etag")
        second = adapter.fetch(source, state, client, now=FROZEN_NOW)
        probe.mode = "timeout"
        failed = adapter.fetch(source, state, client, now=FROZEN_NOW)
        probe.mode = "ok"
        state.etag = None
        recovered = adapter.fetch(source, state, client, now=FROZEN_NOW)
    guardrails = {
        "conditional_get": second.not_modified,
        "timeout_is_typed_failure": failed.error == "timeout",
        "recovery_returns_all_documents": len(recovered.documents) == len(first.documents),
    }

    # The actual Collector must isolate a failed source, recover it and not duplicate rows.
    operational_transport = OperationalTransport()
    # The captured recovery fixture spans more than the production bootstrap
    # day. Give this explicit fault scenario a 72h window so it measures outage
    # recovery, not an unrelated recency filter.
    operational_cfg = replace(
        cfg, scraper=replace(cfg.scraper, concurrency=2, date_window_hours=72)
    )
    with tempfile.TemporaryDirectory(prefix="b1-d-") as tmp:
        operational_paths = ProjectPaths.from_root(tmp)
        db = Database(":memory:")
        try:
            good = db.sources.add(
                Source(
                    name="captured-good",
                    url="https://example.ru/good.xml",
                    fetch_url="https://example.ru/good.xml",
                    kind="rss",
                )
            )
            recovering = db.sources.add(
                Source(
                    name="captured-recovering",
                    url="https://example.org/recover.xml",
                    fetch_url="https://example.org/recover.xml",
                    kind="rss",
                )
            )
            collector = Collector(
                operational_cfg,
                operational_paths,
                db,
                transport=operational_transport.transport(),
                now=lambda: FROZEN_NOW,
            )
            first_run = collector.run(source_ids=[good.id, recovering.id])
            after_first = db.documents.count()
            second_run = collector.run(source_ids=[good.id, recovering.id])
            after_recovery = db.documents.count()
            third_run = collector.run(source_ids=[good.id, recovering.id])
            after_repeat = db.documents.count()
        finally:
            db.close()
    operational = {
        "failed_source_did_not_stop_other": first_run.sources_ok == 1 and first_run.sources_fail == 1 and after_first == 4,
        "recovery_backfilled_available_documents": second_run.sources_fail == 0 and after_recovery == 6,
        "repeat_created_no_duplicates": third_run.docs_new == 0 and after_repeat == after_recovery,
        "documents_after_first": after_first,
        "documents_after_recovery": after_recovery,
        "documents_after_repeat": after_repeat,
    }
    return {
        "benchmark": "B1-D",
        "manifest_version": manifest["version"],
        "captured_at": manifest["captured_at"],
        "generated_at": datetime.now(UTC).isoformat(),
        "scenario_count": len(rows),
        "passed": sum(row["passed"] for row in rows),
        "recall_percent": _percent(sum(row["observed"] for row in rows), sum(row["expected"] for row in rows)),
        "required_field_accuracy_percent": _percent(
            sum(row["observed"] * len(next(s for s in manifest["scenarios"] if s["id"] == row["id"])["required_fields"]) - len(row["field_failures"]) for row in rows),
            sum(row["observed"] * len(next(s for s in manifest["scenarios"] if s["id"] == row["id"])["required_fields"]) for row in rows),
        ),
        "guardrails": guardrails,
        "operational": operational,
        "rows": rows,
    }


def _source_from_dict(raw: dict, source_id: int) -> Source:
    return Source(
        id=source_id,
        name=raw["name"],
        url=raw["url"],
        fetch_url=raw.get("fetch_url") or raw["url"],
        kind=raw.get("kind", "html"),
        category=raw.get("category", "media"),
        enabled=raw.get("enabled", True),
        notes=raw.get("notes", ""),
    )


def _run_collection_sample(
    sources: list[Source], cfg: Config, tavily_key: str | None, sample_per_source: int
) -> dict:
    """Run the real Collector in an isolated DB, capped to avoid crawling whole sites."""
    sample_cfg = replace(
        cfg,
        scraper=replace(
            cfg.scraper,
            fetch_fulltext=True,
            max_new_per_source=max(1, sample_per_source),
        ),
    )
    with tempfile.TemporaryDirectory(prefix="b1-collection-") as tmp:
        paths = ProjectPaths.from_root(tmp)
        db = Database(":memory:")
        stored = []
        try:
            for source in sources:
                stored.append(db.sources.add(replace(source, id=None)))
            collection_started = time.perf_counter()
            report = Collector(sample_cfg, paths, db, tavily_key=tavily_key).run(
                source_ids=[source.id for source in stored if source.id is not None]
            )
            collection_wall_ms = round((time.perf_counter() - collection_started) * 1000, 1)
            rows = []
            for source, entry in zip(stored, sorted(report.per_source, key=lambda x: x["id"])):
                docs = db.documents.list(source_id=source.id, limit=sample_per_source)
                rows.append(
                    {
                        "id": source.id,
                        "name": source.name,
                        "kind": source.kind,
                        "domain": urlsplit(source.fetch_url).hostname or source.kind,
                        "status": entry["status"],
                        "error": entry.get("error"),
                        "candidates_seen": entry["seen"],
                        "documents_stored": entry["new"],
                        "documents_with_content": sum((row["text_len"] or 0) > 0 for row in docs),
                        "latency_adapter_ms": entry["latency_ms"],
                    }
                )
        finally:
            db.close()
    stored_count = sum(row["documents_stored"] for row in rows)
    content_count = sum(row["documents_with_content"] for row in rows)
    return {
        "sample_per_source": sample_per_source,
        "sources": len(rows),
        "sources_ok": sum(row["status"] in ("ok", "not_modified") for row in rows),
        "sources_failed": sum(row["status"] == "failed" for row in rows),
        "documents_stored": stored_count,
        "documents_with_content": content_count,
        "content_percent": _percent(content_count, stored_count),
        "wall_time_ms": collection_wall_ms,
        "rows": rows,
        "note": "Изолированный первый collect; максимум документов на источник задан sample_per_source.",
    }


def run_live(timeout: float = 12.0, workers: int = 8, fulltext_sample: int = 1) -> dict:
    cfg = _config(timeout)
    paths = ProjectPaths.from_root(str(ROOT))
    tavily_key = load_env_secret(cfg.tavily.api_key_env, paths.env_path)
    limiter = HostLimiter(cfg.scraper.per_host_concurrency)
    adapters = build_adapters(cfg, limiter, tavily_key=tavily_key)
    raw_sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]
    sources = [_source_from_dict(raw, index) for index, raw in enumerate(raw_sources, start=1)]
    now = datetime.now(UTC)

    def probe(source: Source) -> dict:
        host = urlsplit(source.fetch_url).hostname or source.kind
        started = time.perf_counter()
        try:
            with make_client(cfg) as client:
                result = adapters[source.kind].fetch(
                    source,
                    FetchState(source_id=source.id or 0),
                    client,
                    now=now,
                    since=now - timedelta(days=7),
                )
        except Exception as exc:  # benchmark records adapter crashes as evidence
            result = None
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = result.error
        latency = round((time.perf_counter() - started) * 1000, 1)
        docs = result.documents if result else []
        return {
            "id": source.id,
            "name": source.name,
            "configured_enabled": source.enabled,
            "kind": source.kind,
            "category": source.category,
            "domain": host,
            "fetch_url": source.fetch_url,
            "status": "ok" if error is None else ("skipped" if "no Tavily API key" in error else "failed"),
            "error": error,
            "documents": len(docs),
            "with_title": sum(bool(d.title) for d in docs),
            "with_text_or_summary": sum(bool(d.text or d.summary) for d in docs),
            "with_published_at": sum(bool(d.published_at) for d in docs),
            "latency_ms": latency,
        }

    rows = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(probe, source): source for source in sources}
        for future in as_completed(futures):
            rows.append(future.result())
    rows.sort(key=lambda row: row["id"])
    attempted = [row for row in rows if row["status"] != "skipped"]
    succeeded = [row for row in attempted if row["status"] == "ok"]
    domains: dict[str, dict] = {}
    for row in rows:
        item = domains.setdefault(row["domain"], {"sources": 0, "ok": 0, "failed": 0, "documents": 0})
        item["sources"] += 1
        item["ok"] += row["status"] == "ok"
        item["failed"] += row["status"] == "failed"
        item["documents"] += row["documents"]
    collection_sample = _run_collection_sample(sources, cfg, tavily_key, fulltext_sample)
    return {
        "benchmark": "B1-R-live-snapshot",
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": 7,
        "timeout_seconds": timeout,
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "configured_sources": len(rows),
        "unique_domains": len(domains),
        "unique_network_domains": len([domain for domain in domains if domain != "search"]),
        "attempted": len(attempted),
        "succeeded": len(succeeded),
        "failed": sum(row["status"] == "failed" for row in rows),
        "skipped": sum(row["status"] == "skipped" for row in rows),
        "source_success_percent": _percent(len(succeeded), len(attempted)),
        "candidates_observed": sum(row["documents"] for row in rows),
        "latency_ms": {
            "median": round(statistics.median(row["latency_ms"] for row in attempted), 1) if attempted else None,
            "p95": round(sorted(row["latency_ms"] for row in attempted)[max(0, int(len(attempted) * 0.95) - 1)], 1) if attempted else None,
            "max": max((row["latency_ms"] for row in attempted), default=None),
        },
        "domains": domains,
        "rows": rows,
        "collection_sample": collection_sample,
        "limitations": [
            "Это одномоментный source-surface probe, а не семидневный абсолютный recall.",
            "Адаптеры проверены до выдачи кандидатов; полное извлечение текста измеряется отдельно.",
            "Внешняя недоступность из текущей сети не равна дефекту парсера.",
        ],
    }


def coverage_report(live: dict | None = None) -> dict:
    raw = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]
    kinds: dict[str, int] = {}
    for source in raw:
        kinds[source["kind"]] = kinds.get(source["kind"], 0) + 1
    gaps = [
        {"method": "arbitrary-json-api", "priority": "high", "reason": "есть только специальный Tavily JSON client; универсального API-коннектора нет"},
        {"method": "js-rendered-pages", "priority": "high", "reason": "нет browser/headless fallback для JS challenge и client-side rendering"},
        {"method": "pdf-docx-attachments", "priority": "high", "reason": "вложения сохраняются ссылками, но текст документов не извлекается"},
        {"method": "authenticated-and-paywalled", "priority": "medium", "reason": "нет cookies/session/subscription transport"},
        {"method": "email-newsletters", "priority": "medium", "reason": "нет IMAP/email ingestion"},
        {"method": "vk-max-other-social", "priority": "medium", "reason": "кроме Telegram специализированных социальных адаптеров нет"},
        {"method": "ocr-images-scans", "priority": "medium", "reason": "нет OCR для сканов и изображений НПА"},
        {"method": "webhook-push", "priority": "low", "reason": "есть только polling; push/webhook источники не поддержаны"},
    ]
    return {
        "configured_by_kind": kinds,
        "implemented": ["rss-atom", "telegram-preview", "telegram-mtproto", "sitemap", "html-link-diff", "tavily-search", "manual-url-import"],
        "gaps": gaps,
        "live_failed_domains": sorted({r["domain"] for r in (live or {}).get("rows", []) if r["status"] == "failed"}),
    }


def _write(name: str, payload: dict) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="B1 Collection benchmark")
    parser.add_argument("mode", choices=("deterministic", "live", "all"), default="all", nargs="?")
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--fulltext-sample", type=int, default=1)
    args = parser.parse_args()
    deterministic = run_deterministic() if args.mode in ("deterministic", "all") else None
    live = (
        run_live(args.timeout, args.workers, args.fulltext_sample)
        if args.mode in ("live", "all")
        else None
    )
    output = {
        "deterministic": deterministic,
        "live": live,
        "coverage": coverage_report(live),
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = _write(f"b1_{stamp}.json", output)
    _write("latest.json", output)
    print(json.dumps({"report": str(path), "summary": output}, ensure_ascii=False, indent=2))
    if deterministic and (
        deterministic["passed"] != deterministic["scenario_count"]
        or not all(deterministic["guardrails"].values())
        or not all(value for key, value in deterministic["operational"].items() if isinstance(value, bool))
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
