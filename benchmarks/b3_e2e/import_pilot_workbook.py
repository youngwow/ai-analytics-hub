#!/usr/bin/env python3
"""Export the human-pilot observation sheet from XLSX to evaluator JSONL."""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NUMERIC = {
    "active_seconds",
    "waiting_seconds",
    "corrections",
    "original_opens",
    "critical_missed",
    "required_facts_missed",
    "unsupported_claims",
    "relevant_items_missed",
    "irrelevant_items_kept",
}
INTEGER = NUMERIC - {"active_seconds", "waiting_seconds"}
REQUIRED = {
    "run_id",
    "participant_id",
    "scenario_id",
    "workflow",
    "task",
    "active_seconds",
    "corrections",
    "completed",
}


def _column(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if letters is None:
        raise ValueError(f"invalid cell reference: {reference}")
    result = 0
    for char in letters.group(0):
        result = result * 26 + ord(char) - 64
    return result - 1


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    value = cell.find(f"{{{MAIN}}}v")
    raw = "" if value is None or value.text is None else value.text
    if kind == "s" and raw:
        return shared[int(raw)]
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{MAIN}}}t"))
    return raw


def read_observations(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relation_id = None
        for sheet in workbook.findall(f".//{{{MAIN}}}sheet"):
            if sheet.get("name") == "Наблюдения":
                relation_id = sheet.get(f"{{{REL}}}id")
                break
        if not relation_id:
            raise ValueError("sheet 'Наблюдения' not found")

        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        target = None
        for relation in relationships.findall(f"{{{PACKAGE_REL}}}Relationship"):
            if relation.get("Id") == relation_id:
                target = str(relation.get("Target") or "").lstrip("/")
                break
        if not target:
            raise ValueError("observation sheet relationship not found")

        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [
                "".join(node.text or "" for node in item.iter(f"{{{MAIN}}}t"))
                for item in strings.findall(f"{{{MAIN}}}si")
            ]
        root = ET.fromstring(archive.read(target))
        rows: dict[int, dict[int, str]] = {}
        for row in root.findall(f".//{{{MAIN}}}row"):
            values = {
                _column(str(cell.get("r"))): _cell_value(cell, shared)
                for cell in row.findall(f"{{{MAIN}}}c")
            }
            rows[int(row.get("r", "0"))] = values

    headers = rows.get(4, {})
    names = {column: value.strip() for column, value in headers.items() if value.strip()}
    result = []
    for number in sorted(key for key in rows if key >= 5):
        values = rows[number]
        item = {name: values.get(column, "").strip() for column, name in names.items()}
        if any(item.values()):
            result.append(item)
    return result


def normalize(rows: list[dict[str, str]]) -> list[dict]:
    output = []
    errors = []
    for index, source in enumerate(rows, start=5):
        row: dict[str, object] = {}
        for key, raw in source.items():
            if not raw:
                continue
            if key in NUMERIC:
                try:
                    number = float(raw.replace(",", "."))
                except ValueError:
                    errors.append(f"row {index}: {key} must be a number")
                    continue
                if key in INTEGER:
                    if not number.is_integer():
                        errors.append(f"row {index}: {key} must be an integer")
                        continue
                    row[key] = int(number)
                else:
                    row[key] = number
            elif key == "completed":
                value = raw.strip().lower()
                if value not in {"true", "false", "1", "0", "да", "нет"}:
                    errors.append(f"row {index}: completed must be TRUE or FALSE")
                else:
                    row[key] = value in {"true", "1", "да"}
            else:
                row[key] = raw
        missing = sorted(REQUIRED - set(row))
        if missing:
            errors.append(f"row {index}: fill {', '.join(missing)}")
        output.append(row)
    if errors:
        raise ValueError("; ".join(errors))
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    rows = normalize(read_observations(args.workbook))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(json.dumps({"rows": len(rows), "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
