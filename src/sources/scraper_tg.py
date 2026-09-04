"""Telegram adapter: MTProto when a session exists, `t.me/s/<channel>` otherwise.

The web preview (scraper.md §3) needs no credentials: the page lists ~20 posts and
paginates with `?after=<id>` (newer) and `?before=<id>` (older). Channels that
disable the preview redirect to `t.me/<channel>` — reported as an error, not as
"no new posts".

MTProto (`telegram_mtproto.py`) reads those same channels through the user's
account: it sees preview-less channels, catches up further than five pages and
knows the names of attached files. Both transports number posts identically, so
`cursor.last_post_id` and the stored `external_id`s carry over either way and
`telegram.mtproto: auto | off | only` switches between them freely.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from urllib.parse import urlsplit

import httpx
from selectolax.parser import HTMLParser, Node

from ..common import get_logger, parse_datetime, to_utc_iso
from ..config import ScraperConfig, TelegramConfig
from ..models import FetchResult, FetchState, RawDocument, Source
from .base import HostLimiter, fetch
from .telegram_mtproto import (
    LOGIN_HINT,
    MtprotoError,
    MtprotoReader,
    MtprotoUnavailable,
    post_to_document,
)
from .textutil import html_to_text, title_from_text

log = get_logger("telegram")

PREVIEW_UNAVAILABLE = "web preview unavailable for this channel (needs MTProto / Telethon)"
NO_CREDENTIALS = f"telegram.mtproto is 'only' but there is no Telegram session: {LOGIN_HINT}"
MAX_INCREMENTAL_PAGES = 5  # ?after= pages per run before we give up catching up
_POST_RE = re.compile(r"^(?P<channel>[A-Za-z0-9_]+)/(?P<id>\d+)$")
_CHANNEL_RE = re.compile(r"/(?:s/)?(?P<channel>[A-Za-z0-9_]{4,32})/?$")


@dataclass
class ChannelPage:
    documents: list[RawDocument]
    title: str = ""
    error: str | None = None
    post_ids: list[int] | None = None


def parse_post_id(data_post: str | None) -> int | None:
    m = _POST_RE.match(data_post or "")
    return int(m.group("id")) if m else None


def _text_of(node: Node | None) -> str:
    return html_to_text(node.html) if node is not None else ""


def _external_links(node: Node | None) -> list[str]:
    if node is None:
        return []
    out: list[str] = []
    for a in node.css("a[href]"):
        href = a.attributes.get("href") or ""
        if href.startswith("http") and "t.me/" not in href:
            out.append(href)
    return out


def parse_message(
    msg: Node, source_id: int, channel_title: str, now: datetime
) -> RawDocument | None:
    """One `.tgme_widget_message` → RawDocument, or None for media-only/service posts."""
    data_post = msg.attributes.get("data-post") or ""
    post_id = parse_post_id(data_post)
    if post_id is None:
        return None
    channel = data_post.split("/", 1)[0]
    text_node = msg.css_first(".tgme_widget_message_text")
    text = _text_of(text_node)
    documents = []
    for a in msg.css("a.tgme_widget_message_document_wrap"):
        href = a.attributes.get("href")
        if href:
            title_node = a.css_first(".tgme_widget_message_document_title")
            documents.append((href, title_node.text(strip=True) if title_node else ""))
    if not text and not documents:
        return None  # photo/video/poll-only post: nothing to read
    attachments = [href for href, _ in documents] + _external_links(text_node)
    attachments = list(dict.fromkeys(attachments))
    date_link = msg.css_first("a.tgme_widget_message_date")
    time_node = date_link.css_first("time") if date_link is not None else None
    published = (
        parse_datetime(time_node.attributes.get("datetime")) if time_node is not None else None
    )
    title = title_from_text(text)
    if not title and documents:
        title = documents[0][1] or f"Документ из {channel_title or channel}"
    return RawDocument(
        source_id=source_id,
        external_id=data_post,
        url=f"https://t.me/{channel}/{post_id}",
        title=title,
        text=text,
        author=channel_title,
        attachments=attachments,
        published_at=to_utc_iso(published),
        fetched_at=to_utc_iso(now) or "",
    )


def parse_channel_page(body: bytes, source_id: int, now: datetime) -> ChannelPage:
    tree = HTMLParser(body)
    header = tree.css_first(".tgme_channel_info_header_title")
    title = header.text(strip=True) if header is not None else ""
    messages = tree.css(".tgme_widget_message[data-post]")
    if not messages and tree.css_first(".tgme_channel_info") is None:
        return ChannelPage(documents=[], title=title, error=PREVIEW_UNAVAILABLE)
    if not title:
        owner = tree.css_first(".tgme_widget_message_owner_name")
        title = owner.text(strip=True) if owner is not None else ""
    docs: list[RawDocument] = []
    ids: list[int] = []
    for msg in messages:
        pid = parse_post_id(msg.attributes.get("data-post"))
        if pid is not None:
            ids.append(pid)
        doc = parse_message(msg, source_id, title, now)
        if doc is not None:
            docs.append(doc)
    return ChannelPage(documents=docs, title=title, post_ids=ids)


def channel_name(fetch_url: str) -> str:
    """`https://t.me/s/mintsifry` → `mintsifry` ("" when the URL is not a channel)."""
    m = _CHANNEL_RE.search(urlsplit(fetch_url).path)
    return m.group("channel") if m else ""


class TelegramAdapter:
    kind = "telegram"

    def __init__(
        self,
        config: ScraperConfig,
        tg_config: TelegramConfig,
        limiter: HostLimiter | None = None,
        mtproto_factory: Callable[[], MtprotoReader] | None = None,
    ):
        self.config = config
        self.tg = tg_config
        self.limiter = limiter
        self.mtproto_factory = mtproto_factory
        self._reader: MtprotoReader | None = None
        self._reader_lock = threading.Lock()  # workers poll channels in parallel
        self._mtproto_down = False  # per-run latch: 16 channels must not retry a dead session

    # -- MTProto --

    def _reader_or_none(self) -> MtprotoReader | None:
        """The shared reader, built once however many channels are polled in parallel."""
        if self.mtproto_factory is None or self._mtproto_down:
            return None
        with self._reader_lock:
            if self._reader is None and not self._mtproto_down:
                self._reader = self.mtproto_factory()
            return self._reader

    def _fetch_mtproto(
        self,
        source: Source,
        state: FetchState,
        *,
        now: datetime,
        since: datetime | None,
        backfill: bool,
        reader: MtprotoReader,
    ) -> FetchResult:
        channel = channel_name(source.fetch_url)
        if not channel:
            return FetchResult(error=f"cannot read a channel name from {source.fetch_url}")
        last_id = int(state.cursor.get("last_post_id") or 0)
        source_id = source.id or 0
        if backfill:
            page = reader.channel_posts(channel, limit=self.tg.backfill_posts)
        else:
            # Never ask for more than the collector will store: its per-source cap
            # would otherwise drop posts the cursor has already moved past.
            limit = min(self.tg.max_posts, self.config.max_new_per_source)
            page = reader.channel_posts(
                channel,
                min_id=last_id,
                limit=limit,
                offset_date=None if last_id else since,
            )
        docs = [
            doc
            for doc in (
                post_to_document(p, channel, source_id, page.title, now) for p in page.posts
            )
            if doc is not None
        ]
        seen = [p.id for p in page.posts]
        newest = max([*seen, last_id]) if (seen or last_id) else None
        cursor = dict(state.cursor)
        cursor["transport"] = "mtproto"
        if newest:
            cursor["last_post_id"] = newest
        log.debug("%s: %d posts over MTProto, cursor -> %s", source.name, len(docs), newest)
        return FetchResult(
            documents=docs, state_update={"cursor": cursor}, source_title=page.title or None
        )

    def close(self) -> None:
        """Disconnect the shared MTProto client at the end of a collect run."""
        with self._reader_lock:
            reader, self._reader = self._reader, None
            self._mtproto_down = False
        if reader is not None:
            reader.close()

    def _load(self, client: httpx.Client, url: str, source_id: int, now: datetime) -> ChannelPage:
        page = fetch(client, url, limiter=self.limiter)
        if page.error:
            return ChannelPage(documents=[], error=page.error)
        if page.status >= 400:
            return ChannelPage(documents=[], error=f"HTTP {page.status}")
        if page.redirected_from and not urlsplit(page.url).path.startswith("/s/"):
            return ChannelPage(documents=[], error=PREVIEW_UNAVAILABLE)
        return parse_channel_page(page.body, source_id, now)

    def fetch(
        self,
        source: Source,
        state: FetchState,
        client: httpx.Client,
        *,
        now: datetime,
        since: datetime | None = None,
        backfill: bool = False,
    ) -> FetchResult:
        mode = self.tg.mtproto
        reader = self._reader_or_none() if mode != "off" else None
        if reader is None:
            if mode == "only":
                return FetchResult(error=NO_CREDENTIALS)
            return self._fetch_web(source, state, client, now=now, backfill=backfill)
        try:
            return self._fetch_mtproto(
                source, state, now=now, since=since, backfill=backfill, reader=reader
            )
        except MtprotoUnavailable as e:
            self._mtproto_down = True  # the session, not this channel: stop trying for this run
            if mode == "only":
                return FetchResult(error=f"MTProto: {e}")
            log.warning("MTProto unavailable (%s); falling back to the t.me/s/ preview", e)
        except MtprotoError as e:
            if mode == "only":
                return FetchResult(error=f"MTProto: {e}")
            log.warning("%s: MTProto failed (%s); falling back to the preview", source.name, e)
        return self._fetch_web(source, state, client, now=now, backfill=backfill)

    def _fetch_web(
        self,
        source: Source,
        state: FetchState,
        client: httpx.Client,
        *,
        now: datetime,
        backfill: bool = False,
    ) -> FetchResult:
        base = source.fetch_url.rstrip("/")
        last_id = state.cursor.get("last_post_id")
        source_id = source.id or 0
        docs: list[RawDocument] = []
        seen_ids: list[int] = []
        title = ""

        if last_id and not backfill:
            # Catch up strictly after the cursor; loop in case more than one page is new.
            cursor = int(last_id)
            for _ in range(MAX_INCREMENTAL_PAGES):
                result = self._load(client, f"{base}?after={cursor}", source_id, now)
                if result.error:
                    return FetchResult(error=result.error)
                title = result.title or title
                new_ids = [i for i in (result.post_ids or []) if i > cursor]
                docs.extend(d for d in result.documents if parse_post_id(d.external_id) > cursor)
                seen_ids.extend(new_ids)
                if not new_ids:
                    break
                cursor = max(new_ids)
        else:
            result = self._load(client, base, source_id, now)
            if result.error:
                return FetchResult(error=result.error)
            title = result.title
            docs.extend(result.documents)
            seen_ids.extend(result.post_ids or [])
            pages = self.tg.backfill_pages if backfill else 0
            oldest = min(seen_ids) if seen_ids else None
            for _ in range(pages):
                if oldest is None:
                    break
                older = self._load(client, f"{base}?before={oldest}", source_id, now)
                if older.error or not older.post_ids:
                    break
                docs.extend(older.documents)
                seen_ids.extend(older.post_ids)
                oldest = min(older.post_ids)

        newest = max([*seen_ids, int(last_id or 0)]) if (seen_ids or last_id) else None
        cursor_update = dict(state.cursor)
        cursor_update["transport"] = "web"
        if newest:
            cursor_update["last_post_id"] = newest
        log.debug("%s: %d posts parsed, cursor -> %s", source.name, len(docs), newest)
        return FetchResult(
            documents=docs, state_update={"cursor": cursor_update}, source_title=title or None
        )
