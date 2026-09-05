"""B1 proof runner: exact contract, faults, full Collector replay and live compatibility.

The runner never judges relevance or summary quality. Live endpoints are called
once per pass; concurrency/load comparisons replay preserved bytes locally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.b1_collection.run import FIXTURES, FROZEN_NOW, run_deterministic  # noqa: E402
from benchmarks.b1_technical.run_full import (  # noqa: E402
    ReplayTransport,
    live_capture,
    load_sources,
    materialize_search_artifacts,
    percentile,
)
from benchmarks.b1_technical.run_full import config as technical_config  # noqa: E402
from src.common import load_env_secret  # noqa: E402
from src.config import Config  # noqa: E402
from src.models import FetchState, Source  # noqa: E402
from src.paths import ProjectPaths  # noqa: E402
from src.sources.base import HostLimiter, build_adapters, make_client  # noqa: E402
from src.sources.collector import Collector  # noqa: E402
from src.sources.fulltext import extract  # noqa: E402
from src.storage import Database  # noqa: E402

UTC = timezone.utc
HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"


def _sha_json(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _minimal_config(*, concurrency: int = 4, fulltext: bool = False) -> Config:
    cfg = Config.load(str(ROOT / "config.yaml"))
    return replace(
        cfg,
        scraper=replace(
            cfg.scraper,
            concurrency=concurrency,
            request_timeout=2,
            fulltext_timeout=2,
            fetch_fulltext=fulltext,
        ),
        telegram=replace(cfg.telegram, mtproto="off"),
    )


class SequenceTransport(httpx.BaseTransport):
    """Exact URL routes whose values may be response sequences or exceptions."""

    def __init__(self, routes: dict[str, list[tuple[int, bytes, dict] | Exception]]):
        self.routes = routes
        self.calls: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        replies = self.routes.get(url, [(404, b"not found", {})])
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            if isinstance(reply, httpx.RequestError):
                reply.request = request
            raise reply
        status, body, headers = reply
        return httpx.Response(status, content=body, headers=headers, request=request)


def _feed(title: str, link: str) -> bytes:
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>'
        f"<item><title>{title}</title><link>{link}</link><guid>{link}</guid>"
        "<pubDate>Wed, 02 Sep 2026 10:00:00 +0300</pubDate></item>"
        "</channel></rss>"
    ).encode()


def run_c1() -> dict:
    base = run_deterministic()
    fulltext = extract(
        (FIXTURES / "article_cp1251.html").read_bytes(),
        "https://example.ru/article",
        20_000,
    )
    fulltext_check = {
        "passed": bool(
            fulltext
            and fulltext.title == "Минцифры предложило правила управления M2M SIM-картами"
            and "Министерство цифрового развития" in fulltext.text
            and fulltext.author == "Иван Петров"
            and fulltext.date == "2026-09-02T09:30:00+00:00"
        ),
        "title": fulltext.title if fulltext else None,
        "author": fulltext.author if fulltext else None,
        "date": fulltext.date if fulltext else None,
        "text_sha256": hashlib.sha256((fulltext.text if fulltext else "").encode()).hexdigest(),
    }
    rows_pass = base["passed"] == base["scenario_count"]
    exact_hashes = all(
        row["ids_sha256"] == row["expected_ids_sha256"] for row in base["rows"]
    )
    return {
        "passed": rows_pass
        and exact_hashes
        and base["recall_percent"] == 100.0
        and base["required_field_accuracy_percent"] == 100.0
        and fulltext_check["passed"],
        "exact_scenarios": base,
        "fulltext_extraction": fulltext_check,
    }


def run_c2() -> dict:
    cfg = _minimal_config(fulltext=False)
    adapters = build_adapters(cfg, HostLimiter(cfg.scraper.per_host_concurrency), tavily_key="fixture-key")
    rss_source = Source(id=1, name="fault", url="https://fault.test/feed", fetch_url="https://fault.test/feed", kind="rss")

    fault_rows = []
    for label, reply, expected in [
        ("http_403", (403, b"", {}), "HTTP 403"),
        ("http_429", (429, b"", {}), "HTTP 429"),
        ("http_500", (500, b"", {}), "HTTP 500"),
        ("malformed_body", (200, b"this is not a feed", {"content-type": "text/plain"}), "not a feed"),
    ]:
        transport = SequenceTransport({rss_source.fetch_url: [reply]})
        with make_client(cfg, transport) as client:
            result = adapters["rss"].fetch(
                rss_source, FetchState(source_id=1), client, now=FROZEN_NOW
            )
        fault_rows.append(
            {"scenario": label, "passed": bool(result.error and expected in result.error), "error": result.error}
        )

    # Telegram state must advance and request only newer posts on the second page.
    tg_url = "https://t.me/s/cit_gov"
    tg_transport = SequenceTransport(
        {
            tg_url: [(200, (FIXTURES / "tg_channel.html").read_bytes(), {"content-type": "text/html"})],
            f"{tg_url}?after=1485": [(200, (FIXTURES / "tg_channel_after.html").read_bytes(), {"content-type": "text/html"})],
            f"{tg_url}?after=1501": [
                (
                    200,
                    b'<html><div class="tgme_channel_info"><div class="tgme_channel_info_header_title">cit_gov</div></div></html>',
                    {"content-type": "text/html"},
                )
            ],
        }
    )
    tg_source = Source(id=2, name="tg", url=tg_url, fetch_url=tg_url, kind="telegram")
    with make_client(cfg, tg_transport) as client:
        first = adapters["telegram"].fetch(
            tg_source, FetchState(source_id=2), client, now=FROZEN_NOW
        )
        state = FetchState(source_id=2, cursor=first.state_update["cursor"], last_success_at="x")
        second = adapters["telegram"].fetch(tg_source, state, client, now=FROZEN_NOW)
    telegram_cursor = {
        "passed": (
            first.state_update["cursor"].get("last_post_id") == 1485
            and [doc.external_id for doc in second.documents] == ["cit_gov/1501"]
            and second.state_update["cursor"].get("last_post_id") == 1501
            and tg_transport.calls == [tg_url, f"{tg_url}?after=1485", f"{tg_url}?after=1501"]
        ),
        "calls": tg_transport.calls,
        "first_cursor": first.state_update["cursor"],
        "second_cursor": second.state_update["cursor"],
    }

    # A child failure must be visible as partial through the public Collector report.
    root = "https://site.test/sitemap.xml"
    child = "https://site.test/news.xml"
    root_xml = (
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<sitemap><loc>{child}</loc><lastmod>2026-09-02T10:00:00Z</lastmod></sitemap>"
        "</sitemapindex>"
    ).encode()
    sm_transport = SequenceTransport(
        {root: [(200, root_xml, {"content-type": "application/xml"})], child: [(500, b"", {})]}
    )
    with tempfile.TemporaryDirectory(prefix="b1-c2-") as tmp:
        db = Database(":memory:")
        try:
            source = db.sources.add(Source(name="sitemap", url=root, fetch_url=root, kind="sitemap"))
            report = Collector(
                cfg,
                ProjectPaths.from_root(tmp),
                db,
                transport=sm_transport,
                now=lambda: FROZEN_NOW,
            ).run(source_ids=[source.id])
            partial_entry = report.per_source[0]
        finally:
            db.close()
    child_partial = {
        "passed": partial_entry["status"] == "partial" and "HTTP 500" in partial_entry["warnings"][0],
        "entry": partial_entry,
    }

    base = run_deterministic()
    inherited = {
        **base["guardrails"],
        **{key: value for key, value in base["operational"].items() if isinstance(value, bool)},
    }
    all_checks = [row["passed"] for row in fault_rows] + [
        telegram_cursor["passed"], child_partial["passed"], *inherited.values()
    ]
    return {
        "passed": all(all_checks),
        "http_and_format_faults": fault_rows,
        "telegram_cursor": telegram_cursor,
        "child_sitemap_partial_visible": child_partial,
        "conditional_recovery_isolation_repeat": inherited,
    }


def _latest_valid_capture() -> Path:
    candidates = []
    root = ROOT / "benchmarks" / "b1_technical" / "runs"
    for path in root.iterdir():
        if path.is_dir() and (path / "captures.jsonl").exists() and not (path / "INVALID_RUN.md").exists():
            candidates.append(path)
    if not candidates:
        raise RuntimeError("no valid captured live snapshot")
    return sorted(candidates)[-1]


def _collector_fingerprint(db: Database) -> tuple[str, int]:
    rows = [
        tuple(row)
        for row in db.conn.execute(
            "SELECT s.name,s.kind,d.external_id,d.url,d.title,d.published_at,d.content_hash "
            "FROM documents d JOIN sources s ON s.id=d.source_id "
            "ORDER BY s.name,s.kind,d.external_id"
        )
    ]
    return _sha_json(rows), len(rows)


def run_c3(capture_dir: Path | None = None) -> dict:
    capture_dir = capture_dir or _latest_valid_capture()
    records = [json.loads(line) for line in (capture_dir / "captures.jsonl").read_text().splitlines()]
    sources = [source for source in load_sources() if source.enabled and source.kind != "search"]
    rows = []
    for concurrency in (1, 8, 24):
        cfg = technical_config(sample_per_source=3, timeout=5, concurrency=concurrency)
        transport = ReplayTransport(capture_dir, records)
        with tempfile.TemporaryDirectory(prefix=f"b1-c3-{concurrency}-") as tmp:
            db = Database(":memory:")
            try:
                stored = [db.sources.add(replace(source, id=None)) for source in sources]
                before = resource.getrusage(resource.RUSAGE_SELF)
                cpu0 = time.process_time()
                wall0 = time.perf_counter()
                report = Collector(
                    cfg,
                    ProjectPaths.from_root(tmp),
                    db,
                    transport=transport,
                    tavily_key="",
                    now=lambda: datetime.now(UTC),
                ).run(source_ids=[source.id for source in stored])
                wall_ms = (time.perf_counter() - wall0) * 1000
                cpu_seconds = time.process_time() - cpu0
                replay_calls = transport.calls
                replay_misses = transport.misses
                fingerprint, count = _collector_fingerprint(db)
                after = resource.getrusage(resource.RUSAGE_SELF)
            finally:
                db.close()
        statuses = {entry["status"]: 0 for entry in report.per_source}
        for entry in report.per_source:
            statuses[entry["status"]] = statuses.get(entry["status"], 0) + 1
        rows.append(
            {
                "concurrency": concurrency,
                "wall_ms": round(wall_ms, 3),
                "cpu_seconds": round(cpu_seconds, 4),
                "peak_rss_mb_process": round(max(before.ru_maxrss, after.ru_maxrss) / (1024 * 1024), 2),
                "sources": len(sources),
                "statuses": statuses,
                "documents": count,
                "fingerprint": fingerprint,
                "replay_calls": replay_calls,
                "replay_misses": replay_misses,
                "warnings": sum(len(entry.get("warnings", [])) for entry in report.per_source),
                "fulltext_attempted": sum(entry.get("fulltext_attempted", 0) for entry in report.per_source),
                "fulltext_extracted": sum(entry.get("fulltext_extracted", 0) for entry in report.per_source),
                "fulltext_fallback": sum(entry.get("fulltext_fallback", 0) for entry in report.per_source),
            }
        )
    fingerprints = {row["fingerprint"] for row in rows}
    correct = [row for row in rows if row["replay_misses"] == 0]
    fastest = min(correct, key=lambda row: row["wall_ms"]) if correct else None
    selected = None
    if fastest:
        tied = [row for row in correct if row["wall_ms"] <= fastest["wall_ms"] * 1.10]
        selected = min(tied, key=lambda row: row["concurrency"])["concurrency"]
    return {
        "passed": len(fingerprints) == 1 and len(correct) == len(rows),
        "capture": str(capture_dir.relative_to(ROOT)),
        "active_sources": len(sources),
        "rows": rows,
        "selected_concurrency": selected,
        "selection_rule": "fastest correct; within 10% choose lower concurrency",
    }


def run_live_pass(mode: str, index: int) -> dict:
    all_sources = load_sources()
    if mode == "active":
        sources = [source for source in all_sources if source.enabled and source.kind != "search"]
    elif mode == "registered-disabled":
        sources = [source for source in all_sources if not source.enabled]
    else:
        raise ValueError(mode)
    key = load_env_secret("TAVILY_API", str(ROOT / ".env")) or ""
    cfg = technical_config(sample_per_source=3, timeout=20, concurrency=8)
    stamp = datetime.now(UTC).strftime(f"%Y%m%dT%H%M%SZ_{mode}_{index}")
    run_dir = RUNS / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    live = live_capture(run_dir, cfg, sources, key)
    records = [json.loads(line) for line in (run_dir / "captures.jsonl").read_text().splitlines()]
    search = materialize_search_artifacts(run_dir, records)
    documents = [
        json.loads(line)
        for line in (run_dir / "documents.jsonl").read_text().splitlines()
        if line.strip()
    ]
    quality = {
        "documents": len(documents),
        "missing_source": sum(not row.get("source_name") for row in documents),
        "missing_external_id": sum(not row.get("external_id") for row in documents),
        "missing_url_not_digest": sum(
            not row.get("url") and not str(row.get("external_id", "")).startswith("summary:")
            for row in documents
        ),
        "missing_title": sum(not row.get("title") for row in documents),
        "missing_readable_content": sum(
            not (row.get("text") or row.get("summary")) for row in documents
        ),
    }
    quality["passed"] = not any(
        value for key, value in quality.items() if key.startswith("missing_")
    )
    secrets = []
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(errors="ignore").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            name, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if len(value) >= 6:
                secrets.append((name.strip(), value.encode()))
    secret_files: dict[str, list[str]] = {}
    for path in run_dir.rglob("*"):
        if not path.is_file():
            continue
        body = path.read_bytes()
        found = [name for name, value in secrets if value in body]
        if found:
            secret_files[str(path.relative_to(run_dir))] = found
    result = {
        "mode": mode,
        "index": index,
        "run_dir": str(run_dir.relative_to(ROOT)),
        "live": {key: value for key, value in live.items() if key != "source_rows"},
        "source_rows": live["source_rows"],
        "search_artifacts": search,
        "document_quality": quality,
        "secret_hits": sum(len(names) for names in secret_files.values()),
        "secret_files": secret_files,
    }
    (run_dir / "live_report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def summarize_c4(active: list[dict], diagnostic: dict | None) -> dict:
    rows = [row for run in active for row in run["source_rows"]]
    critical_failures = [
        {"run": run["index"], "name": row["name"], "kind": row["kind"], "error": row.get("error")}
        for run in active
        for row in run["source_rows"]
        if row["status"] == "failed"
    ]
    methods = {}
    for row in rows:
        method = methods.setdefault(
            row["kind"],
            {
                "attempts": 0,
                "ok": 0,
                "partial": 0,
                "failed": 0,
                "stored": 0,
                "warnings": 0,
                "fulltext_attempted": 0,
                "fulltext_extracted": 0,
                "fulltext_fallback": 0,
                "latency_ms": [],
            },
        )
        method["attempts"] += 1
        method[row["status"]] = method.get(row["status"], 0) + 1
        method["stored"] += row["stored"]
        method["warnings"] += len(row.get("warnings", []))
        method["fulltext_attempted"] += row.get("fulltext_attempted", 0)
        method["fulltext_extracted"] += row.get("fulltext_extracted", 0)
        method["fulltext_fallback"] += row.get("fulltext_fallback", 0)
        method["latency_ms"].append(row["latency_ms"])
    for method in methods.values():
        latencies = method.pop("latency_ms")
        method["latency_p50_ms"] = percentile(latencies, 0.5)
        method["latency_p95_ms"] = percentile(latencies, 0.95)
    quality_failures = [
        run["index"] for run in active if not run["document_quality"]["passed"]
    ]
    secret_hits = sum(run["secret_hits"] for run in active) + (
        diagnostic["secret_hits"] if diagnostic else 0
    )
    return {
        "passed": len(active) == 3 and not critical_failures and not quality_failures and secret_hits == 0,
        "active_runs": [{"index": run["index"], "run_dir": run["run_dir"]} for run in active],
        "active_failures": critical_failures,
        "active_document_quality_failures": quality_failures,
        "secret_hits": secret_hits,
        "by_method": methods,
        "registered_disabled_diagnostic": diagnostic,
        "absolute_recall_claimed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="also make three modest active live passes")
    parser.add_argument("--registered-diagnostic", action="store_true")
    args = parser.parse_args()
    contract = json.loads((HERE / "contract.json").read_text())
    expected_hash = (HERE / "CONTRACT_SHA256").read_text().split()[0]
    actual_hash = hashlib.sha256((HERE / "contract.json").read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        raise SystemExit("contract hash mismatch")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir = RUNS / f"{stamp}_proof"
    report_dir.mkdir(parents=True, exist_ok=False)
    c1 = run_c1()
    c2 = run_c2()
    c3 = run_c3()
    active = [run_live_pass("active", index) for index in range(1, 4)] if args.live else []
    diagnostic = run_live_pass("registered-disabled", 1) if args.registered_diagnostic else None
    c4 = summarize_c4(active, diagnostic) if args.live else {"passed": None, "reason": "live not requested"}
    verdict = "PASS" if all(layer["passed"] for layer in (c1, c2, c3, c4)) else (
        "MIXED" if c1["passed"] and c2["passed"] and c3["passed"] else "FAIL"
    )
    report = {
        "metadata": {
            "run_id": stamp,
            "generated_at": datetime.now(UTC).isoformat(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "contract_version": contract["version"],
            "contract_sha256": actual_hash,
        },
        "verdict": verdict,
        "C1_exact_contract": c1,
        "C2_state_fault": c2,
        "C3_full_collector_replay": c3,
        "C4_live_compatibility": c4,
    }
    target = report_dir / "report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (RUNS / "LATEST_PROOF").write_text(report_dir.name + "\n")
    print(json.dumps({"verdict": verdict, "report": str(target), "layers": {k: v["passed"] for k, v in report.items() if k.startswith("C")}}, ensure_ascii=False, indent=2))
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
