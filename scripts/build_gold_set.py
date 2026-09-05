#!/usr/bin/env python3
"""Собрать скелет золотого набора из уже собранных документов.

Стратифицированная выборка по категориям источников. `type` проставляется там,
где он следует из источника (ленты НПА — акты), `priority` остаётся пустым:
это оценка относительно профиля компании, и её ставит аналитик, а не скрипт.
Строки с пустым `priority` не участвуют в метриках (см. src/processing/quality.py).

    uv run python scripts/build_gold_set.py [--size 100] [--out tests/fixtures/gold/gold_set.jsonl]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.paths import DEFAULT_PATHS  # noqa: E402
from src.storage import Database  # noqa: E402

# Ленты, где документ — это акт или проект акта, а не новость о нём.
NPA_HOSTS = ("publication.pravo.gov.ru", "regulation.gov.ru", "sozd.duma.gov.ru")
QUOTAS = {"regulator": 0.4, "media": 0.4, "telegram": 0.2}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument("--out", default=os.path.join("tests", "fixtures", "gold", "gold_set.jsonl"))
    parser.add_argument("--db", default=DEFAULT_PATHS.db_path)
    args = parser.parse_args()

    db = Database(args.db)
    rows = list(
        db.conn.execute(
            "SELECT d.id, d.url, d.title, d.published_at, length(d.text) AS chars, "
            "s.category, s.name AS source_name FROM documents d "
            "JOIN sources s ON s.id = d.source_id "
            "WHERE d.hidden = 0 AND length(d.text) > 400 "
            "ORDER BY d.published_at DESC, d.id DESC"
        )
    )
    db.close()
    if not rows:
        print("в базе нет документов — сначала `collect`", file=sys.stderr)
        return 1

    picked: list[dict] = []
    for category, share in QUOTAS.items():
        quota = round(args.size * share)
        pool = [r for r in rows if r["category"] == category]
        step = max(1, len(pool) // quota) if quota else 1
        for row in pool[::step][:quota]:
            url = row["url"] or ""
            picked.append(
                {
                    "document_id": int(row["id"]),
                    "url": url,
                    "title": (row["title"] or "")[:160],
                    "source": row["source_name"],
                    "type": "npa" if any(h in url for h in NPA_HOSTS) else "",
                    "priority": "",
                    "skip": False,
                    "note": "",
                }
            )

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        for row in picked:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    typed = sum(1 for r in picked if r["type"])
    print(f"{len(picked)} документов → {args.out}")
    print(f"тип проставлен автоматически у {typed}; priority у всех пуст — размечает аналитик")
    print("значения: type = npa|news, priority = high|medium|low, skip = true если суть во вложении")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
