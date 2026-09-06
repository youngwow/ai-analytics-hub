#!/usr/bin/env python3
"""Apply deterministic release-policy changes to a frozen product run.

This deliberately does not rerun the LLM.  It is used when the analysis and
grouping are already accepted and only the lossless user-facing projection has
changed (for example exposing stored unknowns or enforcing the shared critical
core).  The output keeps an explicit pointer to its source run.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def latest_signals(db_path: Path) -> list[dict]:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            """
            SELECT s.payload
            FROM signal_revisions s
            JOIN (
                SELECT signal_id, MAX(revision) AS revision
                FROM signal_revisions
                GROUP BY signal_id
            ) latest
              ON latest.signal_id = s.signal_id
             AND latest.revision = s.revision
            """
        ).fetchall()
        return [json.loads(str(row["payload"])) for row in rows]
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    prediction = json.loads((args.source_run / "prediction.json").read_text(encoding="utf-8"))
    signals = latest_signals(args.source_run / "product.db")
    by_material: dict[str, list[dict]] = {}
    for signal in signals:
        by_material.setdefault(str(signal["material_id"]), []).append(signal)

    for decision in prediction.get("item_decisions", []):
        if decision.get("critical"):
            decision["roles"] = ["PR", "GR", "HEAD"]

    for obj in prediction.get("objects", []):
        members = [
            signal
            for material_id in obj.get("member_ids", [])
            for signal in by_material.get(str(material_id), [])
        ]
        obj["unknowns"] = [
            {"source_item_id": signal["material_id"], "text": unknown}
            for signal in members
            for unknown in signal.get("unknowns", [])
            if unknown
        ]
        obj["research_questions"] = list(
            dict.fromkeys(
                question
                for signal in members
                for question in signal.get("research_questions", [])
                if question
            )
        )
        if obj.get("critical"):
            obj["roles"] = ["PR", "GR", "HEAD"]

    # Existing deliveries remain authoritative for non-critical objects.  Add
    # only missing critical-core recipients, without changing release timing.
    deliveries = prediction.get("deliveries", [])
    delivered = {
        (str(row.get("recipient")), str(object_id))
        for row in deliveries
        for object_id in row.get("object_ids", [])
    }
    for obj in prediction.get("objects", []):
        if not obj.get("critical"):
            continue
        for role in ("PR", "GR", "HEAD"):
            if (role, str(obj["object_id"])) in delivered:
                continue
            deliveries.append(
                {
                    "delivery_type": "urgent_alert",
                    "recipient": role,
                    "object_ids": [obj["object_id"]],
                    "digest_text": f"• {obj['summary']}",
                }
            )

    telemetry = prediction.setdefault("telemetry", {})
    telemetry["architecture"] = (
        str(telemetry.get("architecture") or "") + ";structured_unknowns_projection_v1"
    ).lstrip(";")
    telemetry["reprojected_from"] = str(args.source_run)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(prediction, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
