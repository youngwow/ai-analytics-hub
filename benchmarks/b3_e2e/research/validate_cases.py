#!/usr/bin/env python3
"""Fail fast when the frozen A2 cases no longer match their source evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "benchmarks/b3_e2e/data/a2_v1"


def main() -> int:
    cases_path, manifest_path = DATA / "cases.json", DATA / "manifest.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_path = ROOT / manifest["source"]
    errors: list[str] = []
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != manifest["source_sha256"]:
        errors.append("B1 source checksum changed")
    if hashlib.sha256(cases_path.read_bytes()).hexdigest() != manifest["cases_sha256"]:
        errors.append("case checksum changed")
    if len(cases) != manifest["case_count"]:
        errors.append("case count differs from manifest")
    ids = [row["id"] for row in cases]
    if len(ids) != len(set(ids)):
        errors.append("duplicate case id")
    for row in cases:
        signal, source = row["signal"], row["source"]
        for claim in signal["claims"]:
            if claim["evidence_quote"] not in source["text"]:
                errors.append(f"{row['id']}: initial quote not found in B1 source")
        expected = row["expected_gate"]
        should_run = signal["importance"] in {"high", "critical"} and bool(
            signal["unknowns"] or signal["research_questions"]
        )
        if should_run != (expected == "research"):
            errors.append(f"{row['id']}: gate expectation contradicts input")
    if errors:
        print("\n".join(errors))
        return 1
    print(f"OK: {len(cases)} frozen A2 cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
