#!/usr/bin/env python3
"""Execute the live A2 comparison and retain every query and returned excerpt."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common import load_env_secret  # noqa: E402
from src.config import Config  # noqa: E402
from src.paths import DEFAULT_PATHS  # noqa: E402
from src.processing.llm import build_llm_provider  # noqa: E402
from src.product.contracts import EvidenceClaim, GsLabsContext, SignalDraft  # noqa: E402
from src.product.providers import TavilyResearchSearch  # noqa: E402
from src.product.research import TargetedResearcher  # noqa: E402

DATA = ROOT / "benchmarks/b3_e2e/data/a2_v1/cases.json"
CONTEXT = ROOT / "benchmarks/b3_e2e/data/v2/context_gs_labs.json"


class RecordingSearch:
    def __init__(self, inner):
        self.inner = inner
        self.calls: list[dict] = []

    def search(self, query: str, *, mode: str):
        started = time.monotonic()
        rows = self.inner.search(query, mode=mode)
        self.calls.append(
            {
                "query": query,
                "mode": mode,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "results": [asdict(row) for row in rows],
            }
        )
        return rows


class RecordingLLM:
    def __init__(self, inner):
        self.inner = inner
        self.calls: list[dict] = []

    def complete(self, prompt: str, schema: dict, *, system: str = ""):
        task = "unknown"
        try:
            task = str(json.loads(prompt).get("task") or "unknown")
        except (json.JSONDecodeError, AttributeError):
            pass
        completion = self.inner.complete(prompt, schema, system=system)
        self.calls.append(
            {
                "task": task,
                "response": completion.data,
                "model": completion.model,
                "tokens_in": completion.tokens_in,
                "tokens_out": completion.tokens_out,
                "latency_ms": completion.latency_ms,
            }
        )
        return completion

    def close(self):
        self.inner.close()


def to_signal(raw: dict) -> SignalDraft:
    raw = dict(raw)
    raw["claims"] = tuple(EvidenceClaim(**row) for row in raw.get("claims", []))
    raw["unknowns"] = tuple(raw.get("unknowns", []))
    raw["research_questions"] = tuple(raw.get("research_questions", []))
    raw["recipient_roles"] = tuple(raw.get("recipient_roles", []))
    return SignalDraft(**raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    config = Config.load()
    llm = RecordingLLM(build_llm_provider(
        config.llm, load_env_secret(config.llm.api_key_env, DEFAULT_PATHS.env_path)
    ))
    tavily = TavilyResearchSearch(
        config.tavily,
        load_env_secret(config.tavily.api_key_env, DEFAULT_PATHS.env_path),
    )
    context = GsLabsContext.from_dict(json.loads(CONTEXT.read_text(encoding="utf-8")))
    cases = json.loads(DATA.read_text(encoding="utf-8"))
    rows = []
    started = time.time()
    try:
        for case in cases:
            recorder = RecordingSearch(tavily)
            llm_start = len(llm.calls)
            signal = to_signal(case["signal"])
            baseline = TargetedResearcher(llm, recorder, config.llm.model).research(
                signal, context, enabled=False
            )
            report = TargetedResearcher(llm, recorder, config.llm.model).research(
                signal, context, enabled=True
            )
            retained = "\n".join(
                result["snippet"]
                for call in recorder.calls
                for result in call["results"]
            )
            accepted_quotes = [claim.evidence_quote for claim in report.confirmed_claims]
            rows.append(
                {
                    "case_id": case["id"],
                    "description": case["description"],
                    "expected_gate": case["expected_gate"],
                    "baseline": asdict(baseline),
                    "research": asdict(report),
                    "search_calls": recorder.calls,
                    "llm_calls": llm.calls[llm_start:],
                    "deterministic_checks": {
                        "gate_correct": (report.status != "not_needed") == (case["expected_gate"] == "research"),
                        "all_claim_quotes_retained": all(q in retained for q in accepted_quotes),
                        "accepted_claim_count": len(report.confirmed_claims),
                        "returned_source_count": len(report.evidence),
                        "query_count": len(report.queries),
                        "remaining_unknown_count": len(report.unknowns),
                    },
                }
            )
    finally:
        tavily.close()
        llm.close()
    payload = {
        "benchmark": "B3-A2-targeted-research",
        "dataset": "a2-v1",
        "started_at_unix": started,
        "finished_at_unix": time.time(),
        "generation_provider": "ollama",
        "generation_model": config.llm.model,
        "search_provider": "tavily",
        "cases": rows,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
