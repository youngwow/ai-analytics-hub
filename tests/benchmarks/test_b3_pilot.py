from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "benchmarks" / "b3_e2e"))

from import_pilot_workbook import normalize, read_observations  # noqa: E402
from product_agent import persist_raw_documents  # noqa: E402

from src.product.contracts import PreparedDocument  # noqa: E402
from src.storage import Database  # noqa: E402


def test_pilot_sources_are_traceable_and_idempotent(tmp_path: Path):
    db = Database(str(tmp_path / "pilot.db"))
    documents = [
        PreparedDocument(
            id="A01",
            title="Первый материал",
            text="Полный исходный текст первого материала.",
            source_name="Тестовый регулятор",
            source_type="html",
            source_url="https://example.test/a01",
            published_at="2026-09-01T10:00:00+03:00",
            source_class="regulator",
        ),
        PreparedDocument(
            id="A02",
            title="Второй материал",
            text="Полный исходный текст второго материала.",
            source_name="Тестовый регулятор",
            source_type="html",
            source_url="https://example.test/a02",
            published_at="2026-09-01T11:00:00+03:00",
            source_class="regulator",
        ),
    ]
    try:
        first = persist_raw_documents(db, documents)
        second = persist_raw_documents(db, documents)

        assert first == second
        assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        row = db.conn.execute(
            "SELECT d.text,s.name,f.coverage_status "
            "FROM documents d JOIN sources s ON s.id=d.source_id "
            "JOIN fetch_state f ON f.source_id=s.id WHERE d.external_id='A01'"
        ).fetchone()
        assert row["text"] == documents[0].text
        assert row["name"] == "Тестовый регулятор"
        assert row["coverage_status"] == "complete"
    finally:
        db.close()


def test_pilot_workbook_contains_frozen_cross_over_rows():
    workbook = ROOT / "artifacts" / "b3" / "PILOT_EXCEL_BASELINE_V1.xlsx"
    rows = read_observations(workbook)

    assert len(rows) == 8
    assert {(row["participant_id"], row["scenario_id"], row["workflow"]) for row in rows} == {
        ("participant-1", "A", "manual_baseline"),
        ("participant-1", "B", "product"),
        ("participant-2", "A", "product"),
        ("participant-2", "B", "manual_baseline"),
    }
    try:
        normalize(rows)
    except ValueError as exc:
        assert "active_seconds" in str(exc)
    else:
        raise AssertionError("an unfilled pilot workbook must not produce evidence")
