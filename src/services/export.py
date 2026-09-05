"""Выгрузка среза наружу: RSS для подписки и CSV для отчёта.

Тот же срез, что видит лента, только сериализованный иначе — своего ядра здесь
нет. Заметки аналитика и скрытые карточки не выгружаются никогда: канал уходит
за пределы дашборда, а авторизации в этой версии нет.
"""

from __future__ import annotations

import csv
import io
from email.utils import format_datetime
from xml.etree import ElementTree as ET

from ..utils import parse_datetime

CSV_COLUMNS = (
    "id",
    "type",
    "npa_status",
    "priority",
    "title",
    "summary",
    "tags",
    "published_at",
    "source_name",
    "canonical_url",
)


def to_rss(rows: list[dict], *, title: str, link: str, description: str, generated_at: str) -> str:
    """RSS 2.0: подписать соседний отдел на живой срез ленты."""
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    _text(channel, "title", title)
    _text(channel, "link", link)
    _text(channel, "description", description)
    _text(channel, "language", "ru")
    build_date = _rfc822(generated_at)
    if build_date:
        _text(channel, "lastBuildDate", build_date)
    for row in rows:
        entry = ET.SubElement(channel, "item")
        _text(entry, "title", row.get("title") or "Без заголовка")
        url = row.get("canonical_url") or ""
        if url:
            _text(entry, "link", url)
        guid = ET.SubElement(entry, "guid", {"isPermaLink": "true" if url else "false"})
        guid.text = url or f"item-{row.get('id')}"
        published = _rfc822(row.get("published_at"))
        if published:
            _text(entry, "pubDate", published)
        _text(entry, "description", _summary(row))
        for tag in row.get("tags") or []:
            _text(entry, "category", tag)
        source = row.get("source_name")
        if source:
            _text(entry, "author", source)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(rss, encoding="unicode")


def to_csv(rows: list[dict]) -> str:
    """CSV с BOM: Excel открывает кириллицу без пляски с кодировками."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow(
            [
                row.get("id"),
                row.get("type") or "",
                row.get("npa_status") or "",
                row.get("priority") or "",
                (row.get("title") or "").replace("\n", " "),
                _summary(row),
                ", ".join(row.get("tags") or []),
                row.get("published_at") or "",
                row.get("source_name") or "",
                row.get("canonical_url") or "",
            ]
        )
    return "﻿" + buffer.getvalue()


def _summary(row: dict) -> str:
    return (row.get("summary") or "").replace("\n", " ").strip()


def _text(parent: ET.Element, tag: str, value: str) -> None:
    ET.SubElement(parent, tag).text = value


def _rfc822(value: str | None) -> str:
    """RSS требует дату по RFC 822, в базе всё в ISO."""
    moment = parse_datetime(value) if value else None
    return format_datetime(moment) if moment else ""
