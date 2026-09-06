#!/usr/bin/env python3
"""Build gold-free A/B product databases for the human cross-over pilot."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from product_agent import persist_raw_documents, prepared_documents


def copy_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp.db")
    temporary.unlink(missing_ok=True)
    with sqlite3.connect(source) as source_db, sqlite3.connect(temporary) as target_db:
        source_db.backup(target_db)
    temporary.replace(destination)
    for suffix in ("-shm", "-wal"):
        Path(str(destination) + suffix).unlink(missing_ok=True)


def prepare_one(run_dir: Path, destination: Path) -> dict[str, int | str]:
    packet = json.loads((run_dir / "agent_input.json").read_text(encoding="utf-8"))
    copy_database(run_dir / "product.db", destination)

    from src.storage import Database

    db = Database(str(destination))
    try:
        documents = prepared_documents(packet)
        raw_ids = persist_raw_documents(db, documents)
        for material_id, raw_id in raw_ids.items():
            db.conn.execute(
                "UPDATE prepared_documents SET raw_document_id=? WHERE material_id=?",
                (raw_id, material_id),
            )
        db.conn.commit()
        missing = int(
            db.conn.execute(
                "SELECT COUNT(*) FROM prepared_documents WHERE raw_document_id IS NULL"
            ).fetchone()[0]
        )
        integrity = str(db.conn.execute("PRAGMA integrity_check").fetchone()[0])
        return {
            "scenario": str(packet["scenario_id"]),
            "documents": len(raw_ids),
            "missing_source_links": missing,
            "integrity": integrity,
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=Path, default=Path("artifacts/b3/final_protocol"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/b3/pilot"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    reports = [
        prepare_one(args.runs / "a_fixed", args.output / "product_A.db"),
        prepare_one(args.runs / "b_fixed", args.output / "product_B.db"),
    ]
    (args.output / "PREPARATION_REPORT.json").write_text(
        json.dumps({"runs": reports}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if any(row["integrity"] != "ok" or row["missing_source_links"] for row in reports):
        return 1
    print(json.dumps({"runs": reports}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
