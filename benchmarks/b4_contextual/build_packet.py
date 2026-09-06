#!/usr/bin/env python3
"""Build a frozen, architecture-blind semantic packet from final B3 outputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from selected_runs import RUNS

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "benchmarks/b4_contextual/data/v1"
B3_DATA = ROOT / "benchmarks/b3_e2e/data/v2"
CONTEXT = ROOT / "project/ЦИФРА/02_НАЙТИ_И_ПРОВЕРИТЬ_СТАВКУ/03_СРЕДА_ЭКСПЕРИМЕНТОВ/CONTEXT_TRUTH_V1"
SCENARIOS = ("A", "B", "D1_critical", "D2_event_boundary", "D3_new_npa", "D4_known_npa")


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def digest_truth(flow: str) -> list[dict]:
    return [
        row for row in rows(B3_DATA / "object_truth.jsonl")
        if row["flow_id"] == flow and row["digest"]
    ]


def prediction(flow: str) -> tuple[Path, dict]:
    path = ROOT / RUNS[flow]
    return path, json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    timeline = {row["id"]: row for row in rows(B3_DATA / "timeline.jsonl")}
    cases: list[dict] = []
    source_paths = [
        B3_DATA / "timeline.jsonl",
        B3_DATA / "ground_truth.jsonl",
        B3_DATA / "object_truth.jsonl",
        CONTEXT / "context.json",
        CONTEXT / "claims.jsonl",
    ]
    sequence = 0
    for flow in SCENARIOS:
        pred_path, pred = prediction(flow)
        source_paths.append(pred_path)
        objects = pred["objects"]
        for truth in digest_truth(flow):
            wanted = set(truth["member_ids"])
            matches = [obj for obj in objects if set(obj["member_ids"]) == wanted]
            sequence += 1
            cases.append(
                {
                    "case_id": f"J{sequence:02d}",
                    "flow": flow,
                    "object_type": truth["type"],
                    "critical": truth["critical"],
                    "member_count": len(wanted),
                    "originals": [
                        {
                            "id": item_id,
                            "title": timeline[item_id]["title"],
                            "text": timeline[item_id]["raw_text"],
                            "source_name": timeline[item_id]["source_name"],
                            "source_url": timeline[item_id]["source_url"],
                        }
                        for item_id in truth["member_ids"]
                    ],
                    "expected": {
                        "ideal_summary": truth["ideal_summary"],
                        "must_preserve": truth["must_preserve"],
                        "roles": truth["roles"],
                    },
                    "candidate": (
                        {
                            "member_ids": matches[0]["member_ids"],
                            "summary": matches[0]["summary"],
                            "claims": matches[0].get("claims", []),
                            "roles": matches[0].get("roles", []),
                            "unknowns": matches[0].get("unknowns", []),
                            "research_questions": matches[0].get("research_questions", []),
                        }
                        if len(matches) == 1 else None
                    ),
                    "match_count": len(matches),
                }
            )
    packet = {
        "benchmark": "B4-contextual-v1",
        "truth_boundary": "team-authored synthetic gold plus frozen team reconstruction of GS Labs context; not customer labels",
        "context": json.loads((CONTEXT / "context.json").read_text(encoding="utf-8")),
        "cases": cases,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    packet_path = DATA / "judge_packet.json"
    packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    source_paths.append(packet_path)
    manifest = {
        "version": "b4-v1",
        "case_count": len(cases),
        "sources": {
            str(path.relative_to(ROOT)): sha(path)
            for path in source_paths
        },
    }
    (DATA / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{packet_path}: {len(cases)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
