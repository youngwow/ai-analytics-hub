#!/usr/bin/env python3
"""Run the B3 semantic judge packet through a disclosed model."""

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

from src.common import load_env_secret  # noqa: E402
from src.config import Config  # noqa: E402
from src.paths import DEFAULT_PATHS  # noqa: E402
from src.processing.llm import build_llm_provider  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("packet", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--meta", type=Path)
    parser.add_argument(
        "--model",
        default="deepseek-v4-flash:cloud",
        choices=[
            "glm-5.3-flash:cloud",
            "deepseek-v4-flash:cloud",
            "gpt-oss:120b-cloud",
        ],
    )
    args = parser.parse_args(argv)

    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    instructions = str(packet.pop("instructions"))
    schema = packet.pop("response_schema")
    expected_ids = [
        row["object_id"] for row in packet.get("expected_objects", [])
    ]
    instructions += (
        "\nВерни ровно один JSON-объект с английскими именами полей из "
        "response_contract. Не переименовывай поля, не оборачивай ответ и не добавляй markdown. "
        "object_results должен содержать ровно по одной строке для каждого required_expected_object_id."
    )
    config = Config.load()
    key = load_env_secret(config.llm.api_key_env, DEFAULT_PATHS.env_path)
    judge_config = replace(
        config.llm,
        model=args.model,
        temperature=0.0,
        think=None,
        max_output_tokens=-1,
    )
    provider = build_llm_provider(judge_config, key)
    started = time.monotonic()
    try:
        completion = provider.complete(
            json.dumps(
                {
                    "required_expected_object_ids": expected_ids,
                    "response_contract": schema,
                    "evaluation_input": packet,
                },
                ensure_ascii=False,
            ),
            schema,
            system=instructions,
        )
    finally:
        provider.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(completion.data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    meta_path = args.meta or args.output.with_suffix(".meta.json")
    meta_path.write_text(
        json.dumps(
            {
                "provider": "ollama",
                "model": args.model,
                "independent_from_generation_model": args.model != config.llm.model,
                "limitation": (
                    "LLM judge is an internal proxy, not a GS Labs employee or customer gold."
                ),
                "tokens_in": completion.tokens_in,
                "tokens_out": completion.tokens_out,
                "latency_ms": completion.latency_ms,
                "wall_seconds": time.monotonic() - started,
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
