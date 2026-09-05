"""`FeedQuery` — единственная форма фильтра ленты.

Одна и та же структура приходит и из CLI, и из HTTP, поэтому фильтр описан один
раз и не может разъехаться между поверхностями (принцип III конституции).
Здесь же разбор дат: голая дата — это местные сутки, а в базе всё в UTC.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..common import parse_datetime
from ..models import ITEM_TYPES, NPA_STATUSES, PRIORITIES

ORDERS = ("published", "priority", "processed")
MAX_LIMIT = 200
DEFAULT_LIMIT = 20


class QueryError(ValueError):
    """Фильтр не собрать: машинный код плюс текст для человека."""

    def __init__(self, message: str, code: str = "validation_error"):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FeedQuery:
    q: str = ""
    type: str | None = None
    npa_status: str | None = None
    priority: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    source_ids: tuple[int, ...] = ()
    date_from: datetime | None = None
    date_to: datetime | None = None
    order: str = "published"
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None
    include_hidden: bool = False

    @classmethod
    def build(
        cls,
        *,
        q: str | None = None,
        type: str | None = None,
        npa_status: str | None = None,
        priority=None,
        tags=None,
        source_ids=None,
        date_from: str | None = None,
        date_to: str | None = None,
        order: str = "published",
        limit: int | None = None,
        cursor: str | None = None,
        include_hidden: bool = False,
        timezone_name: str = "Europe/Moscow",
    ) -> "FeedQuery":
        """Собрать и проверить фильтр. Всё, что не проходит, — `QueryError`."""
        priority = tuple(_unique(priority))
        for value in priority:
            _one_of(value, PRIORITIES, "priority")
        if type is not None:
            _one_of(type, ITEM_TYPES, "type")
        if npa_status is not None:
            _one_of(npa_status, NPA_STATUSES, "npa_status")
            if type == "news":
                raise QueryError("npa_status не применяется к type=news")
        _one_of(order, ORDERS, "order")

        limit = DEFAULT_LIMIT if limit is None else int(limit)
        if not 1 <= limit <= MAX_LIMIT:
            raise QueryError(f"limit должен быть в диапазоне 1..{MAX_LIMIT}, получено {limit}")

        zone = ZoneInfo(timezone_name)
        start = _boundary(date_from, zone, end=False)
        end = _boundary(date_to, zone, end=True)
        if start and end and start > end:
            raise QueryError("from не может быть позже to")

        return cls(
            q=(q or "").strip(),
            type=type,
            npa_status=npa_status,
            priority=priority,
            tags=tuple(_unique(tags)),
            source_ids=tuple(int(s) for s in _unique(source_ids)),
            date_from=start,
            date_to=end,
            order=order,
            limit=limit,
            cursor=cursor,
            include_hidden=bool(include_hidden),
        )

    # -- курсор --

    def fingerprint(self) -> str:
        """Отпечаток среза: курсор действителен только с теми же фильтрами."""
        payload = json.dumps(
            [
                self.q,
                self.type,
                self.npa_status,
                sorted(self.priority),
                sorted(self.tags),
                sorted(self.source_ids),
                self.date_from.isoformat() if self.date_from else None,
                self.date_to.isoformat() if self.date_to else None,
                self.order,
                self.include_hidden,
            ],
            ensure_ascii=False,
        )
        return hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()

    def encode_cursor(self, published_at: str | None, item_id: int) -> str:
        raw = json.dumps(
            {"p": published_at, "i": int(item_id), "fp": self.fingerprint()}, ensure_ascii=False
        )
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    def decode_cursor(self) -> tuple[str | None, int] | None:
        """Разобрать курсор и убедиться, что он от этого же среза."""
        if not self.cursor:
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(self.cursor.encode("ascii")))
            position = (payload["p"], int(payload["i"]))
            fingerprint = payload["fp"]
        except (ValueError, KeyError, TypeError, binascii.Error) as e:
            raise QueryError("курсор не разбирается", code="invalid_cursor") from e
        if fingerprint != self.fingerprint():
            # Иначе следующая страница молча отдала бы другой срез.
            raise QueryError("курсор относится к другому набору фильтров", code="invalid_cursor")
        return position


@dataclass
class DocumentQuery:
    """Фильтр списка необработанного: у документа нет ни приоритета, ни типа."""

    source_ids: tuple[int, ...] = ()
    date_from: datetime | None = None
    date_to: datetime | None = None
    q: str = ""
    limit: int = DEFAULT_LIMIT
    rejected: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def build(cls, *, unsupported: dict | None = None, **kwargs) -> "DocumentQuery":
        present = [name for name, value in (unsupported or {}).items() if value]
        if present:
            raise QueryError(
                f"к списку документов неприменимы фильтры карточки: {', '.join(sorted(present))}"
            )
        feed = FeedQuery.build(**kwargs)
        return cls(
            source_ids=feed.source_ids,
            date_from=feed.date_from,
            date_to=feed.date_to,
            q=feed.q,
            limit=feed.limit,
        )


def _unique(values) -> list:
    return list(dict.fromkeys(v for v in (values or []) if v not in (None, "")))


def _one_of(value: str, allowed, name: str) -> None:
    if value not in allowed:
        raise QueryError(f"{name}: ожидалось одно из {list(allowed)}, получено {value!r}")


def _boundary(value: str | None, zone: ZoneInfo, *, end: bool) -> datetime | None:
    """Голая дата — местные сутки целиком; ISO со смещением — как прислано.

    Без этого «за сегодня» молча теряет первые три часа московского утра — ровно
    те, ради которых и делается утренний разбор.
    """
    if not value:
        return None
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            day = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError as e:
            raise QueryError(f"дата не разбирается: {value!r}") from e
        moment = datetime.combine(day, time.min, tzinfo=zone)
        if end:
            moment = moment + timedelta(days=1) - timedelta(microseconds=1)
        return moment.astimezone(timezone.utc)
    parsed = parse_datetime(text)
    if parsed is None:
        raise QueryError(f"дата не разбирается: {value!r}")
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
