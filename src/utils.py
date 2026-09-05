"""Stateless helpers shared across the package: logging, secrets, clock, hashing.

The template calls this module `utils`; it is the only place that touches `logging`
configuration, so every entry point gets the same format.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

_LOGGING_CONFIGURED = False

# Naive timestamps from Russian sites (date-only sitemap lastmod, HTML dates,
# non-RFC pubDates) are assumed to be Moscow time, not UTC.
MOSCOW = ZoneInfo("Europe/Moscow")

_WS_RE = re.compile(r"\s+")


def configure_logging(level: str = "INFO") -> None:
    """Set up root logging once, from an entry point (CLI `main()` or the API `main()`).

    Idempotent and without `force=True` on purpose: a second call must not detach
    handlers somebody else installed (pytest's caplog, uvicorn's own config).
    """
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # httpx logs every request at INFO; that is noise next to our per-source lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # telethon narrates connection setup at INFO; keep only what went wrong.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    _LOGGING_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger with the project's standard stderr format."""
    configure_logging()
    return logging.getLogger(name)


def load_env_secret(var_name: str, env_path: str | None = None) -> str:
    """Return `var_name` from the process environment or an .env file ("" if unset)."""
    val = os.environ.get(var_name, "")
    if val:
        return val
    if env_path and os.path.exists(env_path):
        prefix = f"{var_name}="
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(prefix):
                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if raw:
                        return raw
    return ""


# ── clock ──────────────────────────────────────────────────────────────────


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_iso(dt: datetime | date | None) -> str | None:
    """Canonical storage format: fixed-width `YYYY-MM-DDTHH:MM:SS+00:00`.

    Fixed width keeps SQLite string comparison/ORDER BY correct. Naive values
    are interpreted as Moscow time; a bare date becomes midnight Moscow.
    """
    if dt is None:
        return None
    if not isinstance(dt, datetime):
        dt = datetime(dt.year, dt.month, dt.day)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_datetime(value: str | None) -> datetime | None:
    """Parse ISO-8601 (with or without offset, date-only) or RFC 2822 into an aware datetime.

    Naive results are assumed Moscow time. Returns None for anything unparseable.
    """
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW)
    return dt


def struct_time_to_datetime(st) -> datetime | None:
    """feedparser's `*_parsed` fields are UTC `time.struct_time` values."""
    if not st:
        return None
    try:
        return datetime(*st[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ── text / hashing ─────────────────────────────────────────────────────────


def normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def sha256_text(*parts: str) -> str:
    """Hash whitespace-normalised text parts joined with a separator."""
    joined = "\n".join(normalize_ws(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
