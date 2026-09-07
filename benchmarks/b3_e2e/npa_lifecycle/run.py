#!/usr/bin/env python3
"""Run the deterministic terminal NPA lifecycle experiment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.product.npa import NpaResolution  # noqa: E402
from src.product.store import ProductStore  # noqa: E402
from src.storage import Database  # noqa: E402


def resolution(stage: str, version: str, effective_from: str | None) -> NpaResolution:
    return NpaResolution(
        object_id="NPA-LIFECYCLE",
        member_signal_ids=(f"{version}:s1",),
        external_id="RU-LIFECYCLE-1",
        current_stage=stage,
        current_version=version,
        change_summary=f"stage={stage}",
        effective_from=effective_from,
    )


def run() -> dict:
    db = Database(":memory:")
    store = ProductStore(db.conn)
    try:
        stages = (
            ("public_discussion", "v1", None),
            ("revised_draft", "v2", None),
            ("adopted", "v3", "2026-12-01"),
        )
        for stage, version, effective_from in stages:
            store.save_npa_resolution(
                resolution(stage, version, effective_from),
                {"stage": stage, "version": version},
                source_url=f"https://regulator.example/RU-LIFECYCLE-1/{version}",
            )

        neighbour = NpaResolution(
            object_id="NPA-NEIGHBOUR",
            member_signal_ids=("neighbour:s1",),
            external_id="RU-LIFECYCLE-2",
            current_stage="adopted",
            current_version="v1",
            change_summary="different act",
            effective_from="2027-01-15",
        )
        store.save_npa_resolution(neighbour, {"stage": "adopted", "version": "v1"})

        before = store.npa_archive_status("NPA-LIFECYCLE", as_of="2026-11-30")
        archived_before = store.archive_npa(
            "NPA-LIFECYCLE", as_of="2026-11-30", actor="benchmark"
        )
        on_date = store.npa_archive_status("NPA-LIFECYCLE", as_of="2026-12-01")
        archived_on_date = store.archive_npa(
            "NPA-LIFECYCLE", as_of="2026-12-01", actor="benchmark"
        )
        archived_twice = store.archive_npa(
            "NPA-LIFECYCLE", as_of="2026-12-01", actor="benchmark"
        )

        main = db.conn.execute(
            "SELECT tracked FROM npa_records WHERE id='NPA-LIFECYCLE'"
        ).fetchone()
        neighbour_row = db.conn.execute(
            "SELECT tracked FROM npa_records WHERE id='NPA-NEIGHBOUR'"
        ).fetchone()
        version_rows = db.conn.execute(
            """SELECT version,stage,effective_at FROM npa_versions
               WHERE npa_id='NPA-LIFECYCLE' ORDER BY version"""
        ).fetchall()
        archive_audits = db.conn.execute(
            """SELECT COUNT(*) FROM audit_events
               WHERE event_type='npa.archived' AND object_id='NPA-LIFECYCLE'"""
        ).fetchone()[0]

        checks = {
            "not_eligible_before_effective_date": not before["eligible"],
            "not_archived_before_effective_date": not archived_before,
            "eligible_on_effective_date": on_date["eligible"],
            "archived_on_effective_date": archived_on_date,
            "second_archive_is_noop": not archived_twice,
            "main_removed_from_active_tracking": main["tracked"] == 0,
            "all_three_versions_preserved": len(version_rows) == 3,
            "neighbour_untouched": neighbour_row["tracked"] == 1,
            "single_archive_audit_event": archive_audits == 1,
        }
        return {
            "experiment": "EXP-B3-06",
            "scenario": "adopted_with_future_date_to_archive",
            "verdict": "PASS" if all(checks.values()) else "FAIL",
            "checks": checks,
            "before_effective_date": before,
            "on_effective_date": on_date,
            "preserved_versions": [dict(row) for row in version_rows],
            "archive_audit_events": archive_audits,
            "boundary": (
                "Deterministic lifecycle after official NPA identity, stage and "
                "effective date are resolved; AI extraction is out of scope."
            ),
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
