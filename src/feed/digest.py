"""Рендер выгрузки: срез ленты в текст, который можно отправить руководителю.

Группировка по приоритету и `high` сверху — прямое следствие задачи «утренний
разбор»: читают сверху вниз и останавливаются, когда важное кончилось.
"""

from __future__ import annotations

from ..models import PRIORITIES

TITLES = {"high": "Высокий приоритет", "medium": "Средний приоритет", "low": "Низкий приоритет"}
TYPE_TITLES = {"npa": "НПА", "news": "Новость"}


def group_by_priority(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """`high` → `medium` → `low`; внутри группы порядок приходит уже отсортированным."""
    groups = {p: [] for p in PRIORITIES}
    for row in rows:
        groups.setdefault(row.get("priority", "low"), []).append(row)
    return [(p, groups[p]) for p in PRIORITIES if groups.get(p)]


def to_markdown(rows: list[dict], *, title: str, generated_at: str, notes: dict | None = None) -> str:
    """Дайджест в markdown: заголовок, группы по приоритету, карточки со ссылками."""
    lines = [f"# {title}", "", f"_Сформировано: {generated_at}; материалов: {len(rows)}_", ""]
    for priority, group in group_by_priority(rows):
        lines.append(f"## {TITLES.get(priority, priority)} ({len(group)})")
        lines.append("")
        for row in group:
            kind = TYPE_TITLES.get(row.get("type"), row.get("type", ""))
            status = f", {row['npa_status']}" if row.get("npa_status") else ""
            lines.append(f"### {row.get('title') or 'Без заголовка'}")
            lines.append(f"*{kind}{status}* · {(row.get('published_at') or '')[:10]}"
                         + (f" · {row['source_name']}" if row.get("source_name") else ""))
            lines.append("")
            for sentence in (row.get("summary") or "").split("\n"):
                if sentence.strip():
                    lines.append(sentence.strip())
            if row.get("tags"):
                lines.append("")
                lines.append("Теги: " + ", ".join(row["tags"]))
            if row.get("canonical_url"):
                lines.append("")
                lines.append(f"[Оригинал]({row['canonical_url']})")
            for note in (notes or {}).get(row["id"], []):
                lines.append("")
                lines.append(f"> Заметка: {note}")
            lines.append("")
    return "\n".join(lines).strip() + "\n"
