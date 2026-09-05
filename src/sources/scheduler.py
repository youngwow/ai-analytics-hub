"""Per-source polling schedule — the whole scheduler, in one module.

There is no daemon and no cron (research.md, R-05): `collect --watch` already
loops, so a tick just asks the database who is due. Fifty rows in SQLite cost
microseconds, and a frequent empty tick is cheaper than making a 15-minute
source wait a quarter of an hour.
"""

from __future__ import annotations

from datetime import timedelta

from ..models import Source
from ..utils import parse_datetime, to_utc_iso, utc_now

INTERVAL_SECONDS = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400}
DEFAULT_INTERVAL = "1h"
# Regulators publish rarely but matter within the hour; media flood and can wait.
SUGGESTED_INTERVAL = {"regulator": "1h", "telegram": "1h", "media": "6h", "manual": "24h"}


def interval_seconds(interval: str) -> int:
    return INTERVAL_SECONDS.get(interval, INTERVAL_SECONDS[DEFAULT_INTERVAL])


def suggest_interval(category: str) -> str:
    return SUGGESTED_INTERVAL.get(category, DEFAULT_INTERVAL)


def next_run_at(interval: str, *, now=None) -> str:
    """When a source polled at `now` should be polled again."""
    moment = (now or utc_now()) + timedelta(seconds=interval_seconds(interval))
    return to_utc_iso(moment) or ""


def is_due(source: Source, *, now=None) -> bool:
    # `error` опрашивается наравне с `active`: это отметка о здоровье, а не пауза.
    if source.status not in ("active", "error"):
        return False
    if not source.next_run_at:
        return True
    scheduled = parse_datetime(source.next_run_at)
    return scheduled is None or scheduled <= (now or utc_now())


def due(db, *, now=None, limit: int = 100) -> list[Source]:
    """Sources whose turn has come, oldest schedule first."""
    return db.sources.due(to_utc_iso(now or utc_now()) or "", limit=limit)


def reschedule(db, source: Source, *, now=None) -> str:
    """Move a source to its next slot; returns the new `next_run_at`."""
    moment = next_run_at(source.poll_interval, now=now)
    db.sources.schedule(source.id, moment)
    source.next_run_at = moment
    return moment
