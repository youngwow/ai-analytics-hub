#!/usr/bin/env python3
"""Run B3-SCALE against real embeddings and the production LLM resolvers."""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import sqlite3
import time
from array import array
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from src.common import load_env_secret
from src.config import Config
from src.paths import DEFAULT_PATHS
from src.processing.llm import (
    Completion,
    LLMProvider,
    build_embedding_provider,
    build_llm_provider,
)
from src.product.contracts import EventRecord, EvidenceClaim, PreparedDocument, SignalDraft
from src.product.events import EventLinker
from src.product.npa import NpaResolver

from .build_scale import DEFAULT_OUTPUT, SIZES

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARTIFACTS = ROOT / "artifacts" / "b3" / "scale_v1"


def object_text(row: dict[str, Any]) -> str:
    return f"{row['title']}\n{row['text']}".strip()


class MeteredProvider:
    def __init__(self, inner: LLMProvider):
        self.inner = inner
        self.calls: list[dict[str, int | str]] = []

    def complete(self, prompt: str, schema: dict, *, system: str = "") -> Completion:
        started = time.monotonic()
        try:
            result = self.inner.complete(prompt, schema, system=system)
        except Exception as exc:
            self.calls.append(
                {
                    "prompt_bytes": len(prompt.encode("utf-8")),
                    "system_bytes": len(system.encode("utf-8")),
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "provider_latency_ms": 0,
                    "wall_latency_ms": int((time.monotonic() - started) * 1000),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            raise
        self.calls.append(
            {
                "prompt_bytes": len(prompt.encode("utf-8")),
                "system_bytes": len(system.encode("utf-8")),
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "provider_latency_ms": result.latency_ms,
                "wall_latency_ms": int((time.monotonic() - started) * 1000),
            }
        )
        return result


class EmbeddingIndex:
    def __init__(self, path: Path, model: str, dimensions: int):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.model = model
        self.dimensions = dimensions
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS embeddings (
            object_id TEXT PRIMARY KEY, kind TEXT NOT NULL, position INTEGER NOT NULL,
            text_sha256 TEXT NOT NULL, model TEXT NOT NULL, dimensions INTEGER NOT NULL,
            vector BLOB NOT NULL, norm REAL NOT NULL)"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_kind_position ON embeddings(kind, position)")

    def missing(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for row in rows:
            checksum = hashlib.sha256(object_text(row).encode("utf-8")).hexdigest()
            found = self.conn.execute(
                "SELECT 1 FROM embeddings WHERE object_id=? AND text_sha256=? AND model=? AND dimensions=?",
                (row["id"], checksum, self.model, self.dimensions),
            ).fetchone()
            if not found:
                result.append(row)
        return result

    def save(self, rows: Sequence[dict[str, Any]], vectors: Sequence[Sequence[float]]) -> None:
        payload = []
        for row, vector in zip(rows, vectors):
            values = array("f", vector)
            norm = math.sqrt(sum(value * value for value in values))
            payload.append(
                (
                    row["id"], row["kind"], row["position"],
                    hashlib.sha256(object_text(row).encode("utf-8")).hexdigest(),
                    self.model, self.dimensions, values.tobytes(), norm,
                )
            )
        self.conn.executemany(
            "INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?, ?, ?, ?, ?, ?)", payload
        )
        self.conn.commit()

    def top(self, query: Sequence[float], *, kind: str, size: int, limit: int = 20) -> list[tuple[float, str]]:
        query_values = array("f", query)
        query_norm = math.sqrt(sum(value * value for value in query_values))
        best: list[tuple[float, str]] = []
        cursor = self.conn.execute(
            "SELECT object_id, vector, norm FROM embeddings WHERE kind=? AND position<?",
            (kind, size),
        )
        for object_id, blob, norm in cursor:
            values = array("f")
            values.frombytes(blob)
            score = sum(left * right for left, right in zip(query_values, values)) / (query_norm * norm)
            entry = (score, str(object_id))
            if len(best) < limit:
                heapq.heappush(best, entry)
            elif entry > best[0]:
                heapq.heapreplace(best, entry)
        return sorted(best, reverse=True)

    def close(self) -> None:
        self.conn.close()


def event_signal(case: dict[str, Any]) -> SignalDraft:
    return SignalDraft(
        signal_id=case["id"], material_id=f"MAT-{case['id']}", summary=case["text"],
        claims=(EvidenceClaim(case["text"], case["text"]),), relevance="relevant",
        importance="medium", interest="BOTH", impact=case["text"], urgency="routine",
        confidence=1.0, source_title=case["text"][:100],
    )


def event_records(rows: Sequence[dict[str, Any]]) -> list[EventRecord]:
    return [
        EventRecord(row["id"], row["title"], row["text"], (), (), object_text(row))
        for row in rows
    ]


def npa_signal(case: dict[str, Any]) -> tuple[SignalDraft, PreparedDocument]:
    material_id = f"MAT-{case['id']}"
    signal = SignalDraft(
        signal_id=case["id"], material_id=material_id, summary=case["text"],
        claims=(EvidenceClaim(case["text"], case["text"]),), relevance="relevant",
        importance="high", interest="GR", impact=case["text"], urgency="routine",
        confidence=1.0, source_title=case["text"][:100], kind="npa",
        npa_identifier=case.get("external_id"), npa_stage="unknown",
        npa_version="unknown", npa_change_summary=case["text"],
    )
    document = PreparedDocument(
        id=material_id, title=case["text"][:100], text=case["text"],
        source_name="B3-SCALE official fixture", source_type="api",
        source_url=f"https://regulator.example/{case['id']}",
        published_at="2026-09-06T10:00:00+03:00", direction="GR",
        source_class="official",
    )
    return signal, document


def tracked_npas(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "object_id": row["id"], "external_id": row.get("external_id"),
            "title": row["title"], "current_stage": "unknown",
            "current_version": "v1", "history": [],
        }
        for row in rows
    ]


def resolve_case(
    case: dict[str, Any], candidates: list[dict[str, Any]], provider: MeteredProvider, model: str
) -> tuple[str | None, str, bool]:
    if case["kind"] == "event":
        decision = EventLinker(provider, None, model=model).link(
            event_signal(case), event_records(candidates), mode="full_scan"
        )
        return decision.event_id, decision.relation, decision.needs_human_review
    signal, document = npa_signal(case)
    resolutions = NpaResolver(provider, model=model).resolve(
        [signal], {document.id: document}, tracked_npas(candidates)
    )
    if not resolutions:
        return None, "different", True
    resolution = resolutions[0]
    candidate_ids = {row["id"] for row in candidates}
    linked = resolution.object_id if resolution.object_id in candidate_ids else None
    return linked, "same_npa" if linked else "different", resolution.needs_human_review


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = Config.load()
    embedding_config = replace(config.embeddings, dimensions=args.dimensions)
    embedder = (
        build_embedding_provider(
            embedding_config,
            load_env_secret(config.embeddings.api_key_env, DEFAULT_PATHS.env_path),
        )
        if "embedding_top20" in args.modes
        else None
    )
    llm = build_llm_provider(
        replace(config.llm, temperature=0.0),
        load_env_secret(config.llm.api_key_env, DEFAULT_PATHS.env_path),
    )
    rows = [json.loads(line) for line in (args.data / "bank.jsonl").read_text(encoding="utf-8").splitlines()]
    if args.overlay:
        overlay = json.loads(args.overlay.read_text(encoding="utf-8"))
        by_position = {int(row["position"]): row for row in overlay}
        rows = [by_position.get(int(row["position"]), row) for row in rows]
    cases = json.loads(args.case_file.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    index = (
        EmbeddingIndex(args.output / "embeddings.sqlite", embedding_config.model, args.dimensions)
        if embedder is not None
        else None
    )
    try:
        missing = index.missing(rows[: max(args.sizes)]) if index is not None else []
        indexed_started = time.monotonic()
        for start in range(0, len(missing), args.embedding_batch):
            batch = missing[start : start + args.embedding_batch]
            assert embedder is not None and index is not None
            index.save(batch, embedder.embed([object_text(row) for row in batch]))
            print(f"embedded {min(start + len(batch), len(missing))}/{len(missing)}", flush=True)
        indexing_ms = int((time.monotonic() - indexed_started) * 1000)
        by_id = {row["id"]: row for row in rows}
        results = []
        for size in args.sizes:
            bank = rows[:size]
            by_kind = {
                kind: [row for row in bank if row["kind"] == kind]
                for kind in ("event", "npa")
            }
            selected_cases = [item for item in cases if item["min_size"] <= size]
            if args.cases:
                selected_cases = [item for item in selected_cases if item["id"] in args.cases]
            for case in selected_cases:
                top: list[tuple[float, str]] = []
                retrieval_ms = None
                if embedder is not None and index is not None:
                    query_started = time.monotonic()
                    vector = embedder.embed([case["text"]])[0]
                    top = index.top(vector, kind=case["kind"], size=size, limit=20)
                    retrieval_ms = int((time.monotonic() - query_started) * 1000)
                top_ids = [object_id for _, object_id in top]
                expected = case["expected_object_id"]
                retrieval_hit = expected is None or expected in top_ids
                modes = []
                if "full_scan" in args.modes:
                    modes.append(("full_scan", by_kind[case["kind"]]))
                if "embedding_top20" in args.modes:
                    modes.append(("embedding_top20", [by_id[item] for item in top_ids]))
                for mode, candidates in modes:
                    metered = MeteredProvider(llm)
                    started = time.monotonic()
                    error = None
                    predicted_id = None
                    predicted_relation = "failed"
                    needs_review = True
                    try:
                        predicted_id, predicted_relation, needs_review = resolve_case(
                            case, candidates, metered, config.llm.model
                        )
                    except Exception as exc:  # recorded evidence; one scale point must not erase the run
                        error = f"{type(exc).__name__}: {exc}"
                    provider_errors = [call.get("error") for call in metered.calls if call.get("error")]
                    if provider_errors and error is None:
                        error = str(provider_errors[-1])
                    result = {
                        "size": size, "case_id": case["id"], "kind": case["kind"],
                        "mode": mode, "bank_candidates": len(candidates),
                        "expected_object_id": expected,
                        "expected_relation": case["expected_relation"],
                        "retrieval_hit_at_20": retrieval_hit if mode == "embedding_top20" else None,
                        "retrieval_ms": retrieval_ms if mode == "embedding_top20" else None,
                        "predicted_object_id": predicted_id,
                        "predicted_relation": predicted_relation,
                        "object_correct": predicted_id == expected,
                        "relation_correct": predicted_relation == case["expected_relation"],
                        "needs_human_review": needs_review,
                        "wall_ms": int((time.monotonic() - started) * 1000),
                        "llm_calls": metered.calls,
                        "error": error,
                    }
                    results.append(result)
                    print(json.dumps(result, ensure_ascii=False), flush=True)
        report = {
            "benchmark": "B3-SCALE", "version": "1.0.0",
            "embedding_model": embedding_config.model,
            "embedding_dimensions": args.dimensions,
            "generation_model": config.llm.model,
            "sizes": args.sizes,
            "indexing_ms_for_missing_objects": indexing_ms,
            "result_count": len(results), "results": results,
        }
        (args.output / "raw_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return report
    finally:
        if index is not None:
            index.close()
        for provider in (embedder, llm):
            if provider is None:
                continue
            close = getattr(provider, "close", None)
            if close:
                close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--case-file", type=Path, default=DEFAULT_OUTPUT / "cases.json")
    parser.add_argument("--overlay", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_ARTIFACTS)
    parser.add_argument("--sizes", type=int, nargs="+", default=list(SIZES))
    parser.add_argument("--dimensions", type=int, default=768)
    parser.add_argument("--embedding-batch", type=int, default=64)
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=["full_scan", "embedding_top20"],
        default=["full_scan", "embedding_top20"],
    )
    parser.add_argument("--cases", nargs="+", help="Run only named cases (diagnostic use).")
    args = parser.parse_args()
    invalid = [size for size in args.sizes if size not in SIZES]
    if invalid:
        parser.error(f"sizes must be chosen from {SIZES}: {invalid}")
    run(args)


if __name__ == "__main__":
    main()
