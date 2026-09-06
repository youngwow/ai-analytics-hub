#!/usr/bin/env python3
"""Apply A4 to saved A1 predictions so the control is not regenerated."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.b2_ai.product_system import CountingProvider, material_prediction  # noqa: E402
from src.common import load_env_secret  # noqa: E402
from src.config import Config  # noqa: E402
from src.paths import DEFAULT_PATHS  # noqa: E402
from src.processing.llm import LlmTemporaryError, build_llm_provider  # noqa: E402
from src.product.contracts import (  # noqa: E402
    AnalysisDraft,
    EvidenceClaim,
    PreparedDocument,
    SignalDraft,
)
from src.product.critic import SignalCritic  # noqa: E402


def reconstructed_signal(material_id: str, row: dict) -> SignalDraft | None:
    claims = tuple(
        EvidenceClaim(str(item["text"]), str(item["evidence_quote"]))
        for item in row.get("claims", [])
        if isinstance(item, dict) and item.get("text") and item.get("evidence_quote")
    )
    if not row.get("summary") or not claims:
        return None
    roles = tuple(role for role in row.get("roles", []) if role in {"PR", "GR", "HEAD"})
    if "PR" in roles and "GR" in roles:
        interest = "BOTH"
    elif "GR" in roles:
        interest = "GR"
    elif "PR" in roles:
        interest = "PR"
    else:
        interest = "IRRELEVANT"
    return SignalDraft(
        signal_id=f"{material_id}:critic",
        material_id=material_id,
        summary=str(row["summary"]),
        claims=claims,
        relevance=row.get("relevance", "unknown"),
        importance=row.get("importance", "medium"),
        interest=interest,
        impact="",
        urgency="urgent" if row.get("critical_or_escalate") else "routine",
        confidence=0.5,
        recipient_roles=roles,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    packet = json.loads(args.input.read_text(encoding="utf-8"))
    control = json.loads(args.control.read_text(encoding="utf-8"))
    materials = {row["id"]: row for row in packet["materials"]}
    config = Config.load()
    provider = build_llm_provider(
        config.llm, load_env_secret(config.llm.api_key_env, DEFAULT_PATHS.env_path)
    )
    counted = CountingProvider(provider)
    critic = SignalCritic(counted, model=config.llm.model)
    predictions = []
    failures: list[dict] = []
    attempted_calls = 0
    partial_path = args.output.with_suffix(".partial.json")
    started = time.monotonic()
    try:
        for row in control["material_predictions"]:
            document = PreparedDocument.from_dict(materials[row["id"]])
            signal = reconstructed_signal(row["id"], row)
            if signal is None:
                predictions.append(row)
                continue
            corrected = None
            last_error = ""
            for attempt in range(config.llm.max_retries + 1):
                attempted_calls += 1
                try:
                    corrected = critic.review(document, signal, enabled=True).corrected_signal
                    break
                except LlmTemporaryError as exc:
                    last_error = str(exc)
                    if attempt < config.llm.max_retries:
                        time.sleep(config.llm.retry_backoff * (2**attempt))
            if corrected is None:
                failures.append({"id": row["id"], "error": last_error})
                predictions.append(row)
                partial_path.parent.mkdir(parents=True, exist_ok=True)
                partial_path.write_text(
                    json.dumps(
                        {
                            "status": "running",
                            "processed": len(predictions),
                            "total": len(control["material_predictions"]),
                            "attempted_calls": attempted_calls,
                            "successful_calls": counted.calls,
                            "failures": failures,
                            "material_predictions": predictions,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                continue
            draft = AnalysisDraft(
                material_id=row["id"],
                status="ok",
                signals=(corrected,),
                configuration_id="a4-with-critic",
                model=config.llm.model,
                context_version=str(packet["context"].get("version") or "unversioned"),
                calls=1,
            )
            predictions.append(material_prediction(document, draft))
            partial_path.parent.mkdir(parents=True, exist_ok=True)
            partial_path.write_text(
                json.dumps(
                    {
                        "status": "running",
                        "processed": len(predictions),
                        "total": len(control["material_predictions"]),
                        "attempted_calls": attempted_calls,
                        "successful_calls": counted.calls,
                        "failures": failures,
                        "material_predictions": predictions,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    finally:
        provider.close()
    output = {
        **control,
        "run_id": str(uuid.uuid4()),
        "configuration_id": f"{control['configuration_id']}+a4-with-critic",
        "usage": {
            "model_calls": counted.calls,
            "input_tokens": counted.input_tokens,
            "output_tokens": counted.output_tokens,
            "estimated_cost": 0,
            "currency": "unknown",
            "wall_seconds": time.monotonic() - started,
            "provider_latency_ms": counted.latency_ms,
            "attempted_calls": attempted_calls,
            "scope": "incremental A4 calls only; control A1 output reused unchanged",
        },
        "a4_failures": failures,
        "material_predictions": predictions,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial_path.write_text(
        json.dumps(
            {
                "status": "complete",
                "processed": len(predictions),
                "total": len(control["material_predictions"]),
                "attempted_calls": attempted_calls,
                "successful_calls": counted.calls,
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
