#!/usr/bin/env python3
"""Exercise only the public NPA output contract on one B2 split."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.b2_ai.product_system import (  # noqa: E402
    NPA_MAX_OUTPUT_TOKENS,
    CountingProvider,
    npa_predictions,
)
from src.common import load_env_secret  # noqa: E402
from src.config import Config  # noqa: E402
from src.paths import DEFAULT_PATHS  # noqa: E402
from src.processing.llm import build_llm_provider  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    packet = json.loads(args.input.read_text(encoding="utf-8"))
    config = Config.load()
    llm_config = replace(
        config.llm,
        temperature=0.0,
        think=None,
        max_output_tokens=NPA_MAX_OUTPUT_TOKENS,
    )
    provider = build_llm_provider(
        llm_config, load_env_secret(llm_config.api_key_env, DEFAULT_PATHS.env_path)
    )
    counted = CountingProvider(provider)
    started = time.monotonic()
    try:
        links, states = npa_predictions(
            counted,
            packet["tasks"].get(
                "npa_state_ids", packet["tasks"]["material_ids"]["B2-N"]
            ),
            packet["tasks"]["npa_pairs"],
            {row["id"]: row for row in packet["materials"]},
        )
    finally:
        provider.close()
    output = {
        "dataset_version": packet["dataset_version"],
        "split": packet["split"],
        "usage": {
            "model_calls": counted.calls,
            "input_tokens": counted.input_tokens,
            "output_tokens": counted.output_tokens,
            "wall_seconds": time.monotonic() - started,
            "provider_latency_ms": counted.latency_ms,
            "attempted_calls": counted.attempted_calls,
            "failed_calls": counted.failed_calls,
        },
        "npa_link_predictions": links,
        "npa_state_predictions": states,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
