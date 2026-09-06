"""Full technical B1: live raw capture, black-box collection and replay load tests.

No LLM and no semantic relevance judgments are used. Secrets and cookies are
redacted before request/response metadata is persisted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import resource
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.b1_collection.run import run_deterministic  # noqa: E402
from src.common import load_env_secret  # noqa: E402
from src.config import Config  # noqa: E402
from src.models import FetchResult, FetchState, Source  # noqa: E402
from src.paths import ProjectPaths  # noqa: E402
from src.sources.base import HostLimiter, build_adapters, make_client  # noqa: E402
from src.sources.collector import Collector  # noqa: E402
from src.storage import Database  # noqa: E402

UTC = timezone.utc
HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
SECRET_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return round(ordered[index], 3)


def pct(a: int, b: int) -> float | None:
    return round(a * 100 / b, 2) if b else None


def safe_headers(headers: httpx.Headers) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in SECRET_HEADERS}


def decoded_headers(headers: httpx.Headers | dict[str, str]) -> dict[str, str]:
    """Headers safe for a body already decoded by HTTPX."""
    return {
        key: value
        for key, value in dict(headers).items()
        if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
    }


def request_bytes(request: httpx.Request) -> bytes:
    try:
        return request.content or b""
    except httpx.RequestNotRead:
        return request.read()


class CaptureTransport(httpx.BaseTransport):
    """Record every raw HTTP exchange into a content-addressed artifact store."""

    def __init__(self, run_dir: Path):
        self.inner = httpx.HTTPTransport(retries=0)
        self.run_dir = run_dir
        self.blobs = run_dir / "raw" / "sha256"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.records: list[dict] = []
        self.lock = threading.Lock()

    def _store(self, body: bytes) -> str:
        digest = sha256(body)
        target = self.blobs / digest[:2] / digest
        with self.lock:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
        return digest

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request_body = request_bytes(request)
        started_wall = datetime.now(UTC).isoformat()
        started = time.perf_counter()
        base = {
            "method": request.method,
            "url": str(request.url),
            "request_headers": safe_headers(request.headers),
            "request_body_sha256": self._store(request_body) if request_body else None,
            "request_bytes": len(request_body),
            "started_at": started_wall,
        }
        try:
            response = self.inner.handle_request(request)
            body = response.read()
        except Exception as exc:
            record = {
                **base,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            with self.lock:
                self.records.append(record)
            raise
        record = {
            **base,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "status": response.status_code,
            "response_headers": safe_headers(response.headers),
            "response_body_sha256": self._store(body),
            "response_bytes": len(body),
            "body_representation": "httpx-decoded",
        }
        with self.lock:
            self.records.append(record)
        return httpx.Response(
            response.status_code,
            headers=decoded_headers(response.headers),
            content=body,
            request=request,
            extensions=response.extensions,
        )

    def close(self) -> None:
        self.inner.close()

    def save_index(self) -> Path:
        path = self.run_dir / "captures.jsonl"
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.records),
            encoding="utf-8",
        )
        return path


class ReplayTransport(httpx.BaseTransport):
    """Byte-exact replay selected by method, URL and request-body hash."""

    def __init__(self, run_dir: Path, records: list[dict]):
        self.run_dir = run_dir
        self.routes: dict[tuple[str, str, str | None], list[dict]] = {}
        for row in records:
            if "status" not in row:
                continue
            key = (row["method"], row["url"], row.get("request_body_sha256"))
            self.routes.setdefault(key, []).append(row)
        self.lock = threading.Lock()
        self.calls = 0
        self.misses = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request_bytes(request)
        body_hash = sha256(body) if body else None
        key = (request.method, str(request.url), body_hash)
        candidates = self.routes.get(key)
        if not candidates:
            # GET bodies are always empty; this fallback also tolerates harmless
            # query ordering differences only when the full URL is identical.
            candidates = self.routes.get((request.method, str(request.url), None))
        with self.lock:
            self.calls += 1
            if not candidates:
                self.misses += 1
        if not candidates:
            return httpx.Response(599, content=b"B1 replay miss", request=request)
        row = candidates[-1]
        digest = row["response_body_sha256"]
        blob = self.run_dir / "raw" / "sha256" / digest[:2] / digest
        return httpx.Response(
            row["status"],
            headers=decoded_headers(row.get("response_headers") or {}),
            content=blob.read_bytes(),
            request=request,
        )


def load_sources() -> list[Source]:
    raw = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]
    return [
        Source(
            id=index,
            name=row["name"],
            url=row["url"],
            fetch_url=row.get("fetch_url") or row["url"],
            kind=row["kind"],
            category=row.get("category", "media"),
            enabled=row.get("enabled", True),
            notes=row.get("notes", ""),
        )
        for index, row in enumerate(raw, start=1)
    ]


def config(sample_per_source: int, timeout: float, concurrency: int) -> Config:
    cfg = Config.load(str(ROOT / "config.yaml"))
    return replace(
        cfg,
        scraper=replace(
            cfg.scraper,
            request_timeout=timeout,
            fulltext_timeout=timeout,
            concurrency=concurrency,
            max_new_per_source=sample_per_source,
            fetch_fulltext=True,
        ),
        telegram=replace(cfg.telegram, mtproto="off"),
    )


def source_kind_by_id(sources: list[Source]) -> dict[int, str]:
    return {source.id or 0: source.kind for source in sources}


class BenchmarkSampleAdapter:
    """Explicit B1 live/replay sample boundary; never installed in product collection."""

    def __init__(self, inner, limit: int):
        self.inner = inner
        self.kind = inner.kind
        self.limit = max(1, limit)

    def fetch(self, *args, **kwargs):
        result = self.inner.fetch(*args, **kwargs)
        if result.error or len(result.documents) <= self.limit:
            return result
        return FetchResult(
            documents=result.documents[: self.limit],
            state_update=result.state_update,
            not_modified=result.not_modified,
            source_title=result.source_title,
            # Sampling is part of the benchmark protocol, not a source failure.
            # Keep genuine adapter warnings only so collection health is not
            # reported as "partial" merely because B1 uses a bounded sample.
            warnings=result.warnings,
        )

    def close(self):
        close = getattr(self.inner, "close", None)
        if close:
            close()


def install_sample_boundary(collector: Collector, limit: int) -> Collector:
    collector.adapters = {
        kind: BenchmarkSampleAdapter(adapter, limit)
        for kind, adapter in collector.adapters.items()
    }
    return collector


def materialize_search_artifacts(run_dir: Path, records: list[dict]) -> list[dict]:
    """Expose search request/response JSON without requiring raw-store lookup."""
    target = run_dir / "search_artifacts"
    target.mkdir(exist_ok=True)
    index = []
    for number, row in enumerate(
        (record for record in records if "api.tavily.com" in record.get("url", "")),
        start=1,
    ):
        item = {
            "number": number,
            "method": row["method"],
            "url": row["url"],
            "started_at": row["started_at"],
            "elapsed_ms": row["elapsed_ms"],
            "status": row.get("status"),
            "request_body_sha256": row.get("request_body_sha256"),
            "response_body_sha256": row.get("response_body_sha256"),
        }
        for side in ("request", "response"):
            digest = row.get(f"{side}_body_sha256")
            if not digest:
                continue
            blob = run_dir / "raw" / "sha256" / digest[:2] / digest
            suffix = ".json" if blob.read_bytes().lstrip().startswith((b"{", b"[")) else ".bin"
            filename = f"{number:02d}_{side}{suffix}"
            (target / filename).write_bytes(blob.read_bytes())
            item[f"{side}_file"] = str((target / filename).relative_to(run_dir))
        index.append(item)
    (target / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return index


def live_capture(run_dir: Path, cfg: Config, sources: list[Source], tavily_key: str) -> dict:
    transport = CaptureTransport(run_dir)
    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="b1-full-") as tmp:
        paths = ProjectPaths.from_root(tmp)
        db = Database(":memory:")
        try:
            stored = [db.sources.add(replace(source, id=None)) for source in sources]
            collector = Collector(
                cfg,
                paths,
                db,
                transport=transport,
                tavily_key=tavily_key,
            )
            install_sample_boundary(collector, cfg.scraper.max_new_per_source)
            report = collector.run(
                source_ids=[source.id for source in stored if source.id is not None]
            )
            kind_by_id = source_kind_by_id(stored)
            source_rows = []
            for entry in sorted(report.per_source, key=lambda row: row["id"]):
                rows = db.documents.list(source_id=entry["id"], limit=10_000)
                source_rows.append(
                    {
                        **entry,
                        "kind": kind_by_id[entry["id"]],
                        "stored": len(rows),
                        "with_title": sum(bool(row["title"]) for row in rows),
                        "with_content": sum((row["text_len"] or 0) > 0 or bool(row["summary"]) for row in rows),
                        "with_date": sum(bool(row["published_at"]) for row in rows),
                    }
                )
            documents = [dict(row) for row in db.conn.execute(
                "SELECT d.*, s.name source_name, s.kind source_kind FROM documents d "
                "JOIN sources s ON s.id=d.source_id ORDER BY d.id"
            )]
        finally:
            db.close()
    wall_ms = round((time.perf_counter() - started) * 1000, 3)
    usage_after = resource.getrusage(resource.RUSAGE_SELF)
    capture_path = transport.save_index()
    (run_dir / "documents.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in documents),
        encoding="utf-8",
    )
    (run_dir / "source_results.json").write_text(
        json.dumps(source_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "wall_ms": wall_ms,
        "sources": len(source_rows),
        "sources_ok": sum(row["status"] in ("ok", "not_modified") for row in source_rows),
        "sources_failed": sum(row["status"] == "failed" for row in source_rows),
        "documents_stored": len(documents),
        "documents_with_content": sum(bool(row["text"] or row["summary"]) for row in documents),
        "documents_with_date": sum(bool(row["published_at"]) for row in documents),
        "http_exchanges": len(transport.records),
        "raw_blobs": len(list((run_dir / "raw" / "sha256").glob("*/*"))),
        "response_bytes": sum(row.get("response_bytes", 0) for row in transport.records),
        "user_cpu_seconds": round(usage_after.ru_utime - usage_before.ru_utime, 3),
        "system_cpu_seconds": round(usage_after.ru_stime - usage_before.ru_stime, 3),
        "peak_rss_mb": round(usage_after.ru_maxrss / (1024 * 1024), 2),
        "captures": str(capture_path.relative_to(ROOT)),
        "source_rows": source_rows,
    }


def replay_load(
    run_dir: Path,
    cfg: Config,
    sources: list[Source],
    tavily_key: str,
    records: list[dict],
    levels: list[int],
    repeats: int,
) -> dict:
    now = datetime.now(UTC)
    output: dict[str, list[dict]] = {}
    for kind in sorted({source.kind for source in sources}):
        kind_sources = [source for source in sources if source.kind == kind]
        kind_rows = []
        for workers in levels:
            transport = ReplayTransport(run_dir, records)
            limiter = HostLimiter(max(workers, cfg.scraper.per_host_concurrency))
            adapters = build_adapters(cfg, limiter, tavily_key=tavily_key)

            def one(source: Source) -> dict:
                started = time.perf_counter()
                with make_client(cfg, transport) as client:
                    result = adapters[kind].fetch(
                        source,
                        FetchState(source_id=source.id or 0),
                        client,
                        now=now,
                        since=now - timedelta(days=7),
                    )
                elapsed = (time.perf_counter() - started) * 1000
                fingerprint = sha256(
                    json.dumps(
                        [(d.external_id, d.url, d.title, d.published_at) for d in result.documents],
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode()
                )
                return {
                    "source_id": source.id,
                    "ok": result.error is None,
                    "error": result.error,
                    "documents": len(result.documents),
                    "latency_ms": elapsed,
                    "fingerprint": fingerprint,
                }

            tasks = [source for source in kind_sources for _ in range(repeats)]
            started = time.perf_counter()
            results = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(one, source) for source in tasks]
                for future in as_completed(futures):
                    results.append(future.result())
            wall = time.perf_counter() - started
            fingerprints: dict[int, set[str]] = {}
            for row in results:
                fingerprints.setdefault(row["source_id"], set()).add(row["fingerprint"])
            latencies = [row["latency_ms"] for row in results]
            kind_rows.append(
                {
                    "workers": workers,
                    "requests": len(results),
                    "success_percent": pct(sum(row["ok"] for row in results), len(results)),
                    "stable_sources_percent": pct(sum(len(v) == 1 for v in fingerprints.values()), len(fingerprints)),
                    "replay_misses": transport.misses,
                    "documents": sum(row["documents"] for row in results),
                    "latency_ms": {
                        "p50": percentile(latencies, 0.5),
                        "p95": percentile(latencies, 0.95),
                        "p99": percentile(latencies, 0.99),
                        "max": round(max(latencies), 3) if latencies else None,
                    },
                    "throughput_requests_per_second": round(len(results) / wall, 2) if wall else None,
                }
            )
        output[kind] = kind_rows
    return {"levels": levels, "repeats_per_source": repeats, "by_kind": output}


def aggregate_methods(live: dict, load: dict) -> list[dict]:
    live_by_kind: dict[str, list[dict]] = {}
    for row in live["source_rows"]:
        live_by_kind.setdefault(row["kind"], []).append(row)
    methods = []
    for kind, rows in live_by_kind.items():
        highest = load["by_kind"][kind][-1]
        stored = sum(row["stored"] for row in rows)
        complete = sum(
            min(row["with_title"], row["with_content"], row["with_date"])
            for row in rows
        )
        methods.append(
            {
                "method": kind,
                "configured_sources": len(rows),
                "live_success_percent": pct(sum(row["status"] in ("ok", "not_modified") for row in rows), len(rows)),
                "sources_with_documents_percent": pct(sum(row["stored"] > 0 for row in rows), len(rows)),
                "stored_documents": stored,
                "content_percent": pct(sum(row["with_content"] for row in rows), stored),
                "date_percent": pct(sum(row["with_date"] for row in rows), stored),
                "complete_document_percent": pct(complete, stored),
                "adapter_latency_p50_ms": percentile([row["latency_ms"] for row in rows], 0.5),
                "adapter_latency_p95_ms": percentile([row["latency_ms"] for row in rows], 0.95),
                "load_workers": highest["workers"],
                "load_success_percent": highest["success_percent"],
                "load_stability_percent": highest["stable_sources_percent"],
                "load_p95_ms": highest["latency_ms"]["p95"],
                "load_throughput_rps": highest["throughput_requests_per_second"],
                "replay_misses": highest["replay_misses"],
            }
        )
    methods.sort(
        key=lambda row: (
            -(row["load_success_percent"] or 0),
            -(row["load_stability_percent"] or 0),
            row["load_p95_ms"] or float("inf"),
        )
    )
    for index, row in enumerate(methods, start=1):
        row["replay_stability_speed_rank"] = index

    live_speed = sorted(methods, key=lambda row: row["adapter_latency_p95_ms"] or float("inf"))
    for index, row in enumerate(live_speed, start=1):
        row["live_speed_rank"] = index

    coverage = sorted(
        methods,
        key=lambda row: (-row["stored_documents"], -row["configured_sources"]),
    )
    for index, row in enumerate(coverage, start=1):
        row["technical_coverage_rank"] = index
    return sorted(methods, key=lambda row: row["method"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Full technical B1 benchmark")
    parser.add_argument("--sample-per-source", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--load-levels", default="1,8,24")
    parser.add_argument("--load-repeats", type=int, default=5)
    args = parser.parse_args()

    tavily_key = load_env_secret("TAVILY_API", str(ROOT / ".env"))
    if not tavily_key:
        raise SystemExit("TAVILY_API is required for the full B1 run")
    cfg = config(args.sample_per_source, args.timeout, args.concurrency)
    sources = load_sources()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    metadata = {
        "benchmark": "B1-technical",
        "started_at": datetime.now(UTC).isoformat(),
        "run_id": stamp,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sample_per_source": args.sample_per_source,
        "timeout_seconds": args.timeout,
        "collector_concurrency": args.concurrency,
        "telegram_transport": "web-preview",
        "tavily_enabled": True,
    }
    live = live_capture(run_dir, cfg, sources, tavily_key)
    records = [json.loads(line) for line in (run_dir / "captures.jsonl").read_text(encoding="utf-8").splitlines()]
    search_artifacts = materialize_search_artifacts(run_dir, records)
    levels = [int(value) for value in args.load_levels.split(",")]
    load = replay_load(run_dir, cfg, sources, tavily_key, records, levels, args.load_repeats)
    deterministic = run_deterministic()
    methods = aggregate_methods(live, load)
    report = {
        "metadata": {**metadata, "finished_at": datetime.now(UTC).isoformat()},
        "live": {key: value for key, value in live.items() if key != "source_rows"},
        "search_artifacts": search_artifacts,
        "methods": methods,
        "rankings": {
            "live_speed": [row["method"] for row in sorted(methods, key=lambda row: row["live_speed_rank"])],
            "technical_coverage": [row["method"] for row in sorted(methods, key=lambda row: row["technical_coverage_rank"])],
            "replay_stability_speed": [row["method"] for row in sorted(methods, key=lambda row: row["replay_stability_speed_rank"])],
        },
        "load": load,
        "deterministic_guardrails": deterministic,
        "limitations": [
            "Telegram использует web-preview: MTProto credentials/session отсутствуют в доступном .env.",
            "Техническая полезность означает доставленные документы с содержимым, а не релевантность GS Labs.",
            "Recall по всем live-доменам не заявляется без независимой ручной разметки raw snapshots.",
            "Нагрузочный replay измеряет adapter/ingestion code на локальных байтах, а не пропускную способность внешних сайтов.",
        ],
    }
    (run_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    latest = RUNS / "latest.json"
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (RUNS / "LATEST_RUN").write_text(stamp + "\n", encoding="utf-8")
    print(json.dumps({"run_dir": str(run_dir), "report": report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
