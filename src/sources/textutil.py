"""Stateless text and URL helpers shared by the adapters."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from courlan import clean_url as _courlan_clean_url

# Tracking query params beyond what courlan strips. `from=main` is Vedomosti's
# click-source marker, `erid` is the Russian ad-label token.
_TRACKING_PREFIXES = ("utm_", "at_", "yclid", "ysclid", "gclid", "fbclid", "_openstat")
_TRACKING_NAMES = {"from", "ref", "referrer", "maca", "erid"}

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(
    r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr|blockquote|section|article)\b[^>]*>", re.IGNORECASE
)
_MULTI_NL_RE = re.compile(r"\n{3,}")
_SPACES_RE = re.compile(r"[ \t ]+")

_BOILERPLATE_PATTERNS = [
    re.compile(r"\s*Читать\s+(далее|полностью|дальше)[….]*\s*$", re.IGNORECASE),
    re.compile(r"\s*Подробнее[….]*\s*$", re.IGNORECASE),
    re.compile(r"\s*Read\s+more[….]*\s*$", re.IGNORECASE),
    re.compile(r"\s*The\s+post\s+.+?\s+appeared\s+first\s+on\s+.+?\.?\s*$", re.IGNORECASE),
    re.compile(r"\s*Сообщение\s+.+?\s+появились\s+сначала\s+на\s+.+?\.?\s*$", re.IGNORECASE),
    re.compile(r"\s*[→»]\s*$"),
]


def clean_url(url: str) -> str:
    """Canonical document URL: tracking params and fragment removed, rest untouched.

    Falls back to the input (stripped) when courlan rejects the URL, so a weird
    but real link is never lost.
    """
    url = (url or "").strip()
    if not url:
        return ""
    cleaned = _courlan_clean_url(url) or url
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return cleaned
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_NAMES and not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    return urlunsplit(parts._replace(query=urlencode(kept), fragment=""))


def strip_html(text: str) -> str:
    """Remove tags and unescape entities; whitespace is left as-is."""
    return html.unescape(_TAG_RE.sub("", text or ""))


def html_to_text(fragment: str) -> str:
    """Turn an HTML fragment (RSS `content:encoded`, `turbo:content`) into plain paragraphs."""
    if not fragment:
        return ""
    text = _BLOCK_RE.sub("\n", fragment)
    text = strip_html(text)
    lines = [_SPACES_RE.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)
    return _MULTI_NL_RE.sub("\n\n", text).strip()


def clean_summary(text: str) -> str:
    """Strip feed boilerplate footers («Читать далее», «The post … appeared first on …»)."""
    text = (text or "").strip()
    for pat in _BOILERPLATE_PATTERNS:
        text = pat.sub("", text)
    return text.strip()


def title_from_text(text: str, limit: int = 120) -> str:
    """First non-empty line, cut at a word boundary — for posts that have no title."""
    for line in (text or "").splitlines():
        line = _SPACES_RE.sub(" ", line).strip()
        if not line:
            continue
        if len(line) <= limit:
            return line
        cut = line[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-")
        return (cut or line[:limit]) + "…"
    return ""


def looks_like_html(body: bytes) -> bool:
    """True when a response body is an HTML page rather than a feed/sitemap."""
    head = body.lstrip(b"\xef\xbb\xbf \t\r\n")[:256].lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


_TG_PATH_RE = re.compile(r"^/s/(?P<channel>[A-Za-z0-9_]+)")


def normalized_source_url(url: str) -> str:
    """Canonical spelling of a source address — the key that catches duplicates.

    `https://t.me/rfrit`, `t.me/rfrit/` and `https://t.me/s/rfrit` are one source,
    so the preview prefix and the trailing slash are stripped before comparing.
    """
    if not url:
        return ""
    cleaned = clean_url(url) or url
    parts = urlsplit(cleaned if "://" in cleaned else f"https://{cleaned}")
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    if host == "t.me":
        match = _TG_PATH_RE.match(path)
        if match:
            path = f"/{match.group('channel')}"
    if parts.scheme in ("", "http", "https"):
        # The query is part of the identity: the three pravo.gov.ru feeds share a
        # path and differ only by `block=`. clean_url has already dropped tracking
        # parameters, so what is left is meaningful.
        query = f"?{parts.query}" if parts.query else ""
        return f"{host}{path}{query}".lower()
    return f"{parts.scheme}://{host}{path}{('?' + parts.query) if parts.query else ''}".lower()
