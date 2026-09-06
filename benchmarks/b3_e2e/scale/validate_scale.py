#!/usr/bin/env python3
"""Validate B3-SCALE structure without calling providers."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .build_scale import DEFAULT_OUTPUT, SIZES


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))
    bank = [json.loads(line) for line in (root / "bank.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(bank) == max(SIZES)
    assert [row["position"] for row in bank] == list(range(max(SIZES)))
    assert len({row["id"] for row in bank}) == len(bank)
    by_id = {row["id"]: row for row in bank}
    assert manifest["bank_sizes"] == list(SIZES)
    assert manifest["files"]["bank.jsonl"] == digest(root / "bank.jsonl")
    assert manifest["files"]["cases.json"] == digest(root / "cases.json")
    for case in cases:
        expected = case["expected_object_id"]
        if expected is not None:
            assert expected in by_id
            assert by_id[expected]["position"] < case["min_size"]
            assert by_id[expected]["kind"] == case["kind"]
    coverage = {
        str(size): {
            "cases": sum(case["min_size"] <= size for case in cases),
            "events": sum(row["kind"] == "event" for row in bank[:size]),
            "npas": sum(row["kind"] == "npa" for row in bank[:size]),
        }
        for size in SIZES
    }
    assert all(row["cases"] >= 4 for row in coverage.values())
    result = {"status": "PASS", "objects": len(bank), "cases": len(cases), "coverage": coverage}
    holdout_manifest_path = root / "holdout_manifest.json"
    if holdout_manifest_path.exists():
        holdout = json.loads(holdout_manifest_path.read_text(encoding="utf-8"))
        overlay_path = root / "holdout_overlay.json"
        holdout_cases_path = root / "holdout_cases.json"
        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
        holdout_cases = json.loads(holdout_cases_path.read_text(encoding="utf-8"))
        assert holdout["base_bank_sha256"] == digest(root / "bank.jsonl")
        assert holdout["overlay_sha256"] == digest(overlay_path)
        assert holdout["cases_sha256"] == digest(holdout_cases_path)
        positions = [int(row["position"]) for row in overlay]
        assert len(positions) == len(set(positions))
        overlay_ids = {row["id"] for row in overlay}
        for case in holdout_cases:
            expected = case["expected_object_id"]
            if expected is not None:
                assert expected in overlay_ids
        result["holdout"] = {
            "status": "PASS",
            "overlay_objects": len(overlay),
            "cases": len(holdout_cases),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(validate(args.root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
