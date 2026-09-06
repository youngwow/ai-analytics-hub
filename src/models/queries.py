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

from ..exceptions import InvalidCursorError, QueryError, QueryValidationError  # noqa: F401
from ..utils import parse_datetime
from .domain import ITEM_TYPES, NPA_STATUSES, PRIORITIES

ORDERS = ("published", "priority", "processed")
# Архив: по умолчанию не показывается, но по запросу — вместе с лентой или отдельно.
ARCHIVED_MODES = ("exclude", "include", "only")
# Очередь сортируется по дате публикации или по времени, когда её забрал сбор.
DOCUMENT_ORDERS = ("published", "fetched")
MAX_LIMIT = 200
DEFAULT_LIMIT = 20



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
    archived: str = "exclude"
    # Только карточки с открытым предложением «вероятный дубль» со сходством не
    # ниже этого (0 — любое); None — фильтр не применяется.
    duplicate: float | None = None

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
        archived: str | None = None,
        duplicate=None,
        timezone_name: str = "Europe/Moscow",
    ) -> "FeedQuery":
        """Собрать и проверить фильтр. Всё, что не проходит, — `QueryValidationError`."""
        priority = tuple(_unique(priority))
        for value in priority:
            _one_of(value, PRIORITIES, "priority")
        if type is not None:
            _one_of(type, ITEM_TYPES, "type")
        if npa_status is not None:
            _one_of(npa_status, NPA_STATUSES, "npa_status")
            if type == "news":
                raise QueryValidationError("npa_status не применяется к type=news")
        _one_of(order, ORDERS, "order")
        archived = archived or "exclude"
        _one_of(archived, ARCHIVED_MODES, "archived")

        limit = DEFAULT_LIMIT if limit is None else _int(limit, "limit")
        if not 1 <= limit <= MAX_LIMIT:
            raise QueryValidationError(f"limit должен быть в диапазоне 1..{MAX_LIMIT}, получено {limit}")

        duplicate = _similarity(duplicate)

        zone = ZoneInfo(timezone_name)
        start = _boundary(date_from, zone, end=False)
        end = _boundary(date_to, zone, end=True)
        if start and end and start > end:
            raise QueryValidationError("from не может быть позже to")

        return cls(
            q=(q or "").strip(),
            type=type,
            npa_status=npa_status,
            priority=priority,
            tags=tuple(_unique(tags)),
            source_ids=tuple(_int(s, "source_id") for s in _unique(source_ids)),
            date_from=start,
            date_to=end,
            order=order,
            limit=limit,
            cursor=cursor,
            include_hidden=bool(include_hidden),
            archived=archived,
            duplicate=duplicate,
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
                self.archived,
                self.duplicate,
            ],
            ensure_ascii=False,
        )
        return hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()

    def encode_cursor(self, published_at: str | None, item_id: int) -> str:
        return _encode_cursor(self.fingerprint(), published_at, item_id)

    def decode_cursor(self) -> tuple[str | None, int] | None:
        """Разобрать курсор и убедиться, что он от этого же среза."""
        return _decode_cursor(self.cursor, self.fingerprint())


@dataclass
class DocumentQuery:
    """Фильтр списка необработанного: у документа нет ни приоритета, ни типа."""

    source_ids: tuple[int, ...] = ()
    date_from: datetime | None = None
    date_to: datetime | None = None
    q: str = ""
    limit: int = DEFAULT_LIMIT
    order: str = "published"
    cursor: str | None = None
    rejected: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def build(cls, *, unsupported: dict | None = None, order: str | None = None, **kwargs) -> "DocumentQuery":
        present = [name for name, value in (unsupported or {}).items() if value]
        if present:
            raise QueryValidationError(
                f"к списку документов неприменимы фильтры карточки: {', '.join(sorted(present))}"
            )
        order = order or "published"
        _one_of(order, DOCUMENT_ORDERS, "order")
        # Порядок ленты к документам не применяется, но остальные проверки — те же.
        feed = FeedQuery.build(**kwargs)
        return cls(
            source_ids=feed.source_ids,
            date_from=feed.date_from,
            date_to=feed.date_to,
            q=feed.q,
            limit=feed.limit,
            order=order,
            cursor=feed.cursor,
        )

    @property
    def sort_field(self) -> str:
        """Имя поля строки, по которому строится курсор."""
        return "published_at" if self.order == "published" else "fetched_at"

    # -- курсор: та же схема, что у ленты, но отпечаток только из фильтров документа --

    def fingerprint(self) -> str:
        payload = json.dumps(
            [
                self.q,
                sorted(self.source_ids),
                self.date_from.isoformat() if self.date_from else None,
                self.date_to.isoformat() if self.date_to else None,
                self.order,
            ],
            ensure_ascii=False,
        )
        return hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()

    def encode_cursor(self, published_at: str | None, document_id: int) -> str:
        return _encode_cursor(self.fingerprint(), published_at, document_id)

    def decode_cursor(self) -> tuple[str | None, int] | None:
        return _decode_cursor(self.cursor, self.fingerprint())


def _encode_cursor(fingerprint: str, published_at: str | None, row_id: int) -> str:
    raw = json.dumps({"p": published_at, "i": int(row_id), "fp": fingerprint}, ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str | None, fingerprint: str) -> tuple[str | None, int] | None:
    """Разобрать курсор и убедиться, что он от этого же среза."""
    if not cursor:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        position = (payload["p"], int(payload["i"]))
        stamp = payload["fp"]
    except (ValueError, KeyError, TypeError, binascii.Error) as e:
        raise InvalidCursorError("курсор не разбирается") from e
    if stamp != fingerprint:
        # Иначе следующая страница молча отдала бы другой срез.
        raise InvalidCursorError("курсор относится к другому набору фильтров")
    return position


def _unique(values) -> list:
    return list(dict.fromkeys(v for v in (values or []) if v not in (None, "")))


def _int(value, name: str) -> int:
    """Нечисловой параметр строки запроса — это 400, а не 500 (дефект R-09)."""
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise QueryValidationError(f"{name}: ожидалось целое число, получено {value!r}") from e


def _similarity(value) -> float | None:
    """Порог сходства «вероятного дубля»: доля 0..1 или проценты 1..100; пусто — нет фильтра.

    `duplicate=0.8` и `duplicate=80` — одно и то же: аналитик думает в процентах,
    а в базе лежит косинус.
    """
    if value is None or value == "" or value is False:
        return None
    if value is True:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise QueryValidationError(f"duplicate: ожидалось число 0..1 или проценты, получено {value!r}") from e
    if 1 < number <= 100:
        number /= 100
    if not 0 <= number <= 1:
        raise QueryValidationError(f"duplicate: сходство должно быть в диапазоне 0..1 (или 0..100 %), получено {value!r}")
    return round(number, 4)


def _one_of(value: str, allowed, name: str) -> None:
    if value not in allowed:
        raise QueryValidationError(f"{name}: ожидалось одно из {list(allowed)}, получено {value!r}")


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
            raise QueryValidationError(f"дата не разбирается: {value!r}") from e
        moment = datetime.combine(day, time.min, tzinfo=zone)
        if end:
            moment = moment + timedelta(days=1) - timedelta(microseconds=1)
        return moment.astimezone(timezone.utc)
    parsed = parse_datetime(text)
    if parsed is None:
        raise QueryValidationError(f"дата не разбирается: {value!r}")
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
