#!/usr/bin/env python3
"""B2 adapter for the target product contour.

Consumes only the ground-truth-free packet made by prepare_input.py and writes
the frozen prediction contract. Ground truth is never imported by this module.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(PROJECT_ROOT))

from src.common import load_env_secret  # noqa: E402
from src.config import Config  # noqa: E402
from src.paths import DEFAULT_PATHS  # noqa: E402
from src.processing.llm import (  # noqa: E402
    LLMProvider,
    LlmTemporaryError,
    build_embedding_provider,
    build_llm_provider,
)
from src.product.analysis import PrimaryAnalyzer  # noqa: E402
from src.product.contracts import EventRecord, GsLabsContext, PreparedDocument  # noqa: E402
from src.product.critic import SignalCritic  # noqa: E402
from src.product.events import EventLinker  # noqa: E402
from src.product.review_policy import review_reasons  # noqa: E402


class CountingProvider:
    def __init__(self, provider: LLMProvider):
        self.provider = provider
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.latency_ms = 0
        self.attempted_calls = 0
        self.failed_calls = 0
        self._lock = threading.Lock()

    def complete(self, prompt, schema, *, system=""):
        with self._lock:
            self.attempted_calls += 1
        try:
            result = self.provider.complete(prompt, schema, system=system)
        except Exception:
            with self._lock:
                self.failed_calls += 1
            raise
        with self._lock:
            self.calls += 1
            self.input_tokens += result.tokens_in
            self.output_tokens += result.tokens_out
            self.latency_ms += result.latency_ms
        return result


def material_prediction(document: PreparedDocument, draft) -> dict:
    if not draft.signals:
        relevance = "irrelevant" if draft.status == "irrelevant" else "unknown"
        return {
            "id": document.id,
            "relevance": relevance,
            "importance": "low",
            "critical_or_escalate": False,
            "roles": [],
            "summary": draft.reason or document.title,
            "claims": [],
            "impact": "",
            "review_required": draft.status not in {"irrelevant"},
            "review_reasons": ["missing_grounded_claims"] if draft.status != "irrelevant" else [],
        }
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    primary = max(draft.signals, key=lambda signal: order[signal.importance])
    reasons = tuple(
        dict.fromkeys(reason for signal in draft.signals for reason in review_reasons(signal))
    )
    claims = []
    seen = set()
    roles = []
    impacts = []
    for signal in draft.signals:
        for role in signal.roles:
            if role not in roles:
                roles.append(role)
        if signal.impact and signal.impact not in impacts:
            impacts.append(signal.impact)
        for claim in signal.claims:
            key = (claim.text, claim.evidence_quote)
            if key not in seen:
                claims.append({"text": claim.text, "evidence_quote": claim.evidence_quote})
                seen.add(key)
    return {
        "id": document.id,
        "relevance": primary.relevance,
        "importance": primary.importance,
        "critical_or_escalate": any(signal.critical_or_escalate for signal in draft.signals),
        "roles": roles,
        "summary": " ".join(signal.summary for signal in draft.signals),
        "claims": claims,
        "impact": " ".join(impacts),
        "review_required": bool(reasons),
        "review_reasons": list(reasons),
    }


EVENT_SYSTEM = """Раздели публикации на события. Верни только JSON {"clusters":[{"cluster_id":"...","member_ids":["..."]}]}.
Одна тема не означает одно событие. Объединяй только один объект, действие и совместимую хронологию.
Перепечатки объединяй. Обновление того же события объединяй. Разные мнения об одном событии
объединяй, сохраняя публикации отдельными member_ids. Каждый id должен встретиться ровно один раз."""
EVENT_SCHEMA = {"type": "object", "required": ["clusters"]}

NPA_STAGES = [
    "announcement",
    "draft",
    "revised_draft",
    "introduced",
    "public_discussion",
    "discussion_closed",
    "scheduled_first_reading",
    "returned_for_revision",
    "review_resumed",
    "adopted",
    "effective",
    "annual_update_window",
    "unknown",
]
NPA_RELATIONS = [
    "same_state",
    "next_state",
    "same_stage",
    "stale_republication",
    "third_party_mention",
    "related",
    "different",
]
NPA_MAX_OUTPUT_TOKENS = 1024
NPA_PAIR_SYSTEM = f"""Определи, относятся ли пары записей к одному НПА только по данным материалов.
Верни только JSON {{"pairs":[{{"case_id":"...","same_npa":true,"relation":"next_state"}}]}}.
Общая тема не означает один НПА. Для разных НПА relation=different; для одного НПА выбери точное
отношение. Допустимые relation: {", ".join(NPA_RELATIONS)}."""
NPA_PAIR_SCHEMA = {
    "type": "object",
    "required": ["pairs"],
    "properties": {
        "pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["case_id", "same_npa", "relation"],
                "properties": {
                    "case_id": {"type": "string"},
                    "same_npa": {"type": "boolean"},
                    "relation": {"enum": NPA_RELATIONS},
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}
NPA_STATE_SYSTEM = f"""Определи стадию каждого НПА только по явному действию в тексте.
Верни только JSON {{"states":[{{"id":"...","stage":"draft"}}]}}.
Не определяй стадию по общей теме. Допустимые stage: {", ".join(NPA_STAGES)}."""
NPA_STATE_SCHEMA = {
    "type": "object",
    "required": ["states"],
    "properties": {
        "states": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "stage"],
                "properties": {
                    "id": {"type": "string"},
                    "stage": {"enum": NPA_STAGES},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def event_predictions(linker: EventLinker, ids: list[str], drafts: dict, *, mode: str) -> list[dict]:
    """Run the actual incremental event-linking contour, not a benchmark-only clustering prompt."""
    events: list[EventRecord] = []
    singleton_ids: list[str] = []
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    for item_id in ids:
        draft = drafts[item_id]
        if not draft.signals:
            singleton_ids.append(item_id)
            continue
        signal = max(draft.signals, key=lambda row: order[row.importance])
        decision = linker.link(signal, events, mode=mode)
        if decision.event_id and decision.relation in {"same_event", "event_update"}:
            updated = []
            for event in events:
                if event.id != decision.event_id:
                    updated.append(event)
                    continue
                updated.append(
                    replace(
                        event,
                        material_ids=tuple(dict.fromkeys((*event.material_ids, item_id))),
                        signal_ids=tuple(dict.fromkeys((*event.signal_ids, signal.signal_id))),
                        compact_text=f"{event.compact_text}\n{signal.summary}\n{signal.impact}",
                        embedding=(),
                        version=event.version + 1,
                    )
                )
            events = updated
        else:
            events.append(
                EventRecord(
                    id=f"event-{len(events) + 1}",
                    title=signal.summary,
                    summary=signal.summary,
                    signal_ids=(signal.signal_id,),
                    material_ids=(item_id,),
                    compact_text=f"{signal.summary}\n{signal.impact}",
                )
            )
    clusters = [
        {"cluster_id": event.id, "member_ids": list(event.material_ids)} for event in events
    ]
    clusters.extend(
        {"cluster_id": f"singleton-{item_id}", "member_ids": [item_id]}
        for item_id in singleton_ids
    )
    return clusters


def _chunks(rows: list, size: int):
    for offset in range(0, len(rows), size):
        yield rows[offset : offset + size]


def _npa_call(
    provider: CountingProvider,
    *,
    ids: list[str],
    pairs: list[dict],
    documents: dict[str, dict],
) -> dict:
    relevant = sorted(
        set(ids) | {row["left_id"] for row in pairs} | {row["right_id"] for row in pairs}
    )
    try:
        if pairs:
            payload = {
                "materials": [documents[item_id] for item_id in relevant],
                "pairs": pairs,
            }
            schema, system = NPA_PAIR_SCHEMA, NPA_PAIR_SYSTEM
        else:
            payload = {"materials": [documents[item_id] for item_id in relevant]}
            schema, system = NPA_STATE_SCHEMA, NPA_STATE_SYSTEM
        return provider.complete(
            json.dumps(payload, ensure_ascii=False), schema, system=system
        ).data
    except LlmTemporaryError:
        # Preserve the exact public contract. A failed chunk remains explicitly
        # unknown/different and cannot erase successful predictions in other chunks.
        return {"pairs": [], "states": []}


def npa_predictions(
    provider: CountingProvider,
    ids: list[str],
    pairs: list[dict],
    documents: dict[str, dict],
    *,
    pair_batch_size: int = 1,
    state_batch_size: int = 3,
):
    if not ids and not pairs:
        return [], []
    if pair_batch_size < 1 or state_batch_size < 1:
        raise ValueError("NPA batch sizes must be positive")

    pair_by_id: dict[str, dict] = {}
    for batch in _chunks(pairs, pair_batch_size):
        result = _npa_call(provider, ids=[], pairs=batch, documents=documents)
        expected = {row["case_id"] for row in batch}
        pair_by_id.update(
            {
                str(row.get("case_id")): row
                for row in result.get("pairs", [])
                if isinstance(row, dict) and str(row.get("case_id")) in expected
            }
        )

    state_by_id: dict[str, dict] = {}
    for batch in _chunks(ids, state_batch_size):
        result = _npa_call(provider, ids=batch, pairs=[], documents=documents)
        expected = set(batch)
        state_by_id.update(
            {
                str(row.get("id")): row
                for row in result.get("states", [])
                if isinstance(row, dict) and str(row.get("id")) in expected
            }
        )
    pair_output = []
    for pair in pairs:
        raw = pair_by_id.get(pair["case_id"], {})
        same = bool(raw.get("same_npa", False))
        relation = str(raw.get("relation") or ("related" if same else "different"))
        pair_output.append({"case_id": pair["case_id"], "same_npa": same, "relation": relation})
    states = [{"id": item_id, "stage": str(state_by_id.get(item_id, {}).get("stage") or "unknown")} for item_id in ids]
    return pair_output, states


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--a1", choices=["one_pass", "two_pass"], default="one_pass")
    parser.add_argument("--a3", choices=["full_scan", "embedding_top20"], default="full_scan")
    parser.add_argument("--a4", choices=["without_critic", "with_critic"], default="without_critic")
    parser.add_argument("--configuration-id")
    parser.add_argument("input", nargs="?", default=os.environ.get("B2_INPUT"))
    parser.add_argument("output", nargs="?", default=os.environ.get("B2_OUTPUT"))
    args = parser.parse_args(argv)
    if not args.input or not args.output:
        parser.error("input/output paths or B2_INPUT/B2_OUTPUT are required")
    packet = json.loads(Path(args.input).read_text(encoding="utf-8"))
    config = Config.load()
    key = load_env_secret(config.llm.api_key_env, DEFAULT_PATHS.env_path)
    provider = build_llm_provider(config.llm, key)
    npa_provider = build_llm_provider(
        replace(
            config.llm,
            temperature=0.0,
            think=None,
            max_output_tokens=NPA_MAX_OUTPUT_TOKENS,
        ),
        key,
    )
    embedder = None
    if args.a3 == "embedding_top20":
        embedding_key = load_env_secret(config.embeddings.api_key_env, DEFAULT_PATHS.env_path)
        embedder = build_embedding_provider(config.embeddings, embedding_key)
    counted = CountingProvider(provider)
    npa_counted = CountingProvider(npa_provider)
    analyzer = PrimaryAnalyzer(counted, model=config.llm.model, max_chars=config.processing.max_chars)
    critic = SignalCritic(counted, model=config.llm.model)
    linker = EventLinker(counted, embedder, model=config.llm.model)
    context = GsLabsContext.from_dict(packet["context"])
    documents = {row["id"]: row for row in packet["materials"]}
    material_ids = packet["tasks"]["material_ids"]
    predictions = []
    drafts = {}
    started = time.monotonic()
    try:
        def analyze_item(item_id):
            document = PreparedDocument.from_dict(documents[item_id])
            draft = analyzer.analyze(document, context, mode=args.a1)
            if args.a4 == "with_critic" and draft.signals:
                draft = replace(
                    draft,
                    signals=tuple(
                        critic.review(document, signal, enabled=True).corrected_signal
                        for signal in draft.signals
                    ),
                )
            return item_id, draft, material_prediction(document, draft)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=config.processing.concurrency
        ) as executor:
            for item_id, draft, prediction in executor.map(
                analyze_item, material_ids["B2-F"]
            ):
                drafts[item_id] = draft
                predictions.append(prediction)
        clusters = event_predictions(linker, material_ids["B2-E"], drafts, mode=args.a3)
        pair_predictions, state_predictions = npa_predictions(
            npa_counted,
            packet["tasks"].get("npa_state_ids", material_ids["B2-N"]),
            packet["tasks"]["npa_pairs"],
            documents,
        )
    finally:
        if embedder and hasattr(embedder, "close"):
            embedder.close()
        npa_provider.close()
        provider.close()
    output = {
        "run_id": str(uuid.uuid4()),
        "configuration_id": args.configuration_id or (
            f"product-a1-{args.a1}-a3-{args.a3}-a4-{args.a4}"
        ),
        "dataset_version": packet["dataset_version"],
        "split": packet["split"],
        "usage": {
            "model_calls": counted.calls + npa_counted.calls,
            "input_tokens": counted.input_tokens + npa_counted.input_tokens,
            "output_tokens": counted.output_tokens + npa_counted.output_tokens,
            "estimated_cost": 0,
            "currency": "unknown",
            "wall_seconds": time.monotonic() - started,
            "provider_latency_ms": counted.latency_ms + npa_counted.latency_ms,
            "attempted_calls": counted.attempted_calls + npa_counted.attempted_calls,
            "failed_calls": counted.failed_calls + npa_counted.failed_calls,
        },
        "material_predictions": predictions,
        "event_clusters": clusters,
        "npa_link_predictions": pair_predictions,
        "npa_state_predictions": state_predictions,
    }
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
