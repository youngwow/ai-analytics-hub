"""Telegram over MTProto (Telethon) — the credentialed transport for `telegram` sources.

Everything Telethon lives here. The rest of the code sees plain dataclasses
(`MtPost`, `MtChannel`) behind the sync `MtprotoReader` protocol, so adapters and
tests never touch the library: telethon is imported lazily inside the coroutines,
which also keeps the test suite's import time down.

Telethon is asyncio-only while the collector is a thread pool, so `TelethonReader`
owns one background event loop with one connected client per collect run and hands
work to it with `run_coroutine_threadsafe`. Reading public channels this way is an
ordinary client operation: we never join anything, `iter_messages` does not move the
read pointer, and concurrent history requests are capped by a semaphore.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Protocol

from ..common import get_logger, load_env_secret, to_utc_iso
from ..config import TelegramConfig
from ..models import RawDocument
from ..paths import ProjectPaths
from .textutil import title_from_text

log = get_logger("telegram.mtproto")

LOGIN_HINT = "run `python -m src telegram login`"
NO_SESSION = f"no Telegram session: {LOGIN_HINT}"
FILE_PREFIX = "file:"  # attachments hold URLs; MTProto files have a name but no public URL
DEVICE_MODEL = "ai-analytics-hub"


class MtprotoError(Exception):
    """One channel could not be read (private, gone, flood wait, timeout)."""


class MtprotoUnavailable(MtprotoError):
    """MTProto is unusable for the whole run: no credentials, no session, revoked key."""


@dataclass(frozen=True)
class MtPost:
    """One channel post, already stripped of Telethon types."""

    id: int
    text: str = ""
    date: datetime | None = None
    urls: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


@dataclass
class MtChannel:
    title: str = ""
    posts: list[MtPost] = field(default_factory=list)


class MtprotoReader(Protocol):
    """The seam the adapter depends on; `TelethonReader` is the only real one."""

    def channel_posts(
        self,
        channel: str,
        *,
        min_id: int = 0,
        limit: int = 100,
        offset_date: datetime | None = None,
    ) -> MtChannel:
        """Newest posts of `channel`, oldest first when `min_id`/`offset_date` bound them."""
        ...

    def close(self) -> None:
        """Disconnect; safe to call more than once."""
        ...


@dataclass(frozen=True)
class TelegramCredentials:
    api_id: int
    api_hash: str
    session_path: str


def credentials_from_env(tg: TelegramConfig, paths: ProjectPaths) -> TelegramCredentials | None:
    """Read api_id/api_hash from the environment or `.env`; None when unusable."""
    raw_id = load_env_secret(tg.api_id_env, paths.env_path)
    api_hash = load_env_secret(tg.api_hash_env, paths.env_path)
    if not raw_id or not api_hash:
        return None
    try:
        api_id = int(raw_id)
    except ValueError:
        log.warning("%s is not a number; MTProto disabled", tg.api_id_env)
        return None
    return TelegramCredentials(
        api_id=api_id, api_hash=api_hash, session_path=paths.data(f"{tg.session_name}.session")
    )


# ── posts → documents ──────────────────────────────────────────────────────


def post_to_document(
    post: MtPost, channel: str, source_id: int, channel_title: str, now: datetime
) -> RawDocument | None:
    """One post → RawDocument, or None for media-only/service posts.

    Mirrors `scraper_tg.parse_message` field for field: both transports number
    posts identically, so a source can switch between them without re-importing.
    """
    if not post.text.strip() and not post.files:
        return None  # media-only post: nothing to read
    title = title_from_text(post.text)
    if not title and post.files:
        title = post.files[0] or f"Документ из {channel_title or channel}"
    attachments = [f"{FILE_PREFIX}{name}" for name in post.files if name] + list(post.urls)
    return RawDocument(
        source_id=source_id,
        external_id=f"{channel}/{post.id}",
        url=f"https://t.me/{channel}/{post.id}",
        title=title,
        text=post.text,
        author=channel_title,
        attachments=list(dict.fromkeys(attachments)),
        published_at=to_utc_iso(post.date),
        fetched_at=to_utc_iso(now) or "",
    )


def _message_urls(msg) -> list[str]:
    """External links of a post: entity URLs plus the web preview, minus t.me links."""
    urls: list[str] = []
    try:
        entities = msg.get_entities_text() or []
    except (AttributeError, TypeError):  # not a text message
        entities = []
    for entity, text in entities:
        url = getattr(entity, "url", None) or text or ""
        if url.startswith("http") and "t.me/" not in url:
            urls.append(url)
    preview = getattr(msg, "web_preview", None)
    preview_url = getattr(preview, "url", "") if preview is not None else ""
    if preview_url and "t.me/" not in preview_url:
        urls.append(preview_url)
    return urls


def _message_file(msg) -> str:
    """Attached document's file name; "" for photos, video notes and text-only posts."""
    file = getattr(msg, "file", None)
    return (getattr(file, "name", "") or "") if file is not None else ""


def posts_from_messages(messages) -> list[MtPost]:
    """Telethon messages → `MtPost`s, oldest first, album siblings merged.

    A multi-file post (`grouped_id`) arrives as several messages with the caption
    on one of them; merging keeps every file name on the post that carries the text.
    """
    ordered = sorted(messages, key=lambda m: getattr(m, "id", 0))
    posts: dict[int, MtPost] = {}
    group_head: dict[int, int] = {}
    for msg in ordered:
        if getattr(msg, "action", None) is not None:
            continue  # service message (pinned, joined, …)
        post_id = getattr(msg, "id", 0)
        if not post_id:
            continue
        text = (getattr(msg, "raw_text", "") or "").strip()
        file_name = _message_file(msg)
        urls = _message_urls(msg)
        group = getattr(msg, "grouped_id", None)
        head_id = group_head.get(group) if group else None
        if head_id is not None:
            head = posts[head_id]
            merged = MtPost(
                id=head.id,
                text=head.text or text,
                date=head.date,
                urls=list(dict.fromkeys([*head.urls, *urls])),
                files=list(dict.fromkeys([*head.files, *([file_name] if file_name else [])])),
            )
            posts[head_id] = merged
            continue
        posts[post_id] = MtPost(
            id=post_id,
            text=text,
            date=getattr(msg, "date", None),
            urls=list(dict.fromkeys(urls)),
            files=[file_name] if file_name else [],
        )
        if group:
            group_head[group] = post_id
    return [posts[k] for k in sorted(posts)]


# ── Telethon runner ────────────────────────────────────────────────────────


def _translate(exc: Exception) -> MtprotoError:
    """Map a Telethon exception to our two-level error type."""
    from telethon import errors

    if isinstance(exc, MtprotoError):
        return exc
    if isinstance(
        exc,
        (
            errors.AuthKeyUnregisteredError,
            errors.AuthKeyDuplicatedError,
            errors.SessionRevokedError,
            errors.UserDeactivatedError,
        ),
    ):
        return MtprotoUnavailable(f"Telegram session is no longer valid: {LOGIN_HINT}")
    if isinstance(exc, errors.FloodWaitError):
        return MtprotoError(f"flood wait {exc.seconds}s")
    if isinstance(exc, errors.ChannelPrivateError):
        return MtprotoError("channel is private or the account was banned from it")
    if isinstance(exc, (errors.UsernameNotOccupiedError, errors.UsernameInvalidError)):
        return MtprotoError("no such channel")
    if isinstance(exc, ValueError):  # get_entity's "Cannot find any entity corresponding to …"
        return MtprotoError(str(exc))
    if isinstance(exc, errors.RPCError):
        return MtprotoError(f"{exc.__class__.__name__}: {exc}")
    return MtprotoError(f"{exc.__class__.__name__}: {exc}")


class TelethonReader:
    """`MtprotoReader` backed by one Telethon client on a private event loop."""

    def __init__(
        self,
        credentials: TelegramCredentials,
        *,
        flood_threshold: float = 60.0,
        request_timeout: float = 60.0,
        concurrency: int = 2,
    ):
        self.credentials = credentials
        self.flood_threshold = flood_threshold
        self.request_timeout = request_timeout
        self._sem = threading.Semaphore(max(1, concurrency))
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client = None

    # -- loop plumbing --

    def _submit(self, coro, timeout: float):
        loop = self._loop
        if loop is None:
            raise MtprotoUnavailable("MTProto client is closed")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout)
        except FutureTimeout:
            future.cancel()
            raise MtprotoError(f"timeout after {timeout:.0f}s") from None

    def _ensure_client(self):
        with self._lock:
            if self._client is not None:
                return self._client
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=self._run_loop, args=(loop,), name="telethon-loop", daemon=True
                )
                thread.start()
                self._loop, self._thread = loop, thread
            self._client = self._submit(self._connect(), self.request_timeout)
            return self._client

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    async def _connect(self):
        from telethon import TelegramClient

        session = self.credentials.session_path
        if not os.path.exists(session):
            raise MtprotoUnavailable(NO_SESSION)
        client = TelegramClient(
            session,
            self.credentials.api_id,
            self.credentials.api_hash,
            flood_sleep_threshold=self.flood_threshold,
            device_model=DEVICE_MODEL,
            receive_updates=False,
            connection_retries=2,
            request_retries=2,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                raise MtprotoUnavailable(NO_SESSION)
        except Exception as e:
            raise _translate(e) from e
        log.debug("MTProto connected (%s)", os.path.basename(session))
        return client

    # -- reading --

    def channel_posts(
        self,
        channel: str,
        *,
        min_id: int = 0,
        limit: int = 100,
        offset_date: datetime | None = None,
    ) -> MtChannel:
        client = self._ensure_client()
        with self._sem:
            return self._submit(
                self._history(client, channel, min_id, limit, offset_date), self.request_timeout
            )

    async def _history(self, client, channel: str, min_id: int, limit: int, offset_date) -> MtChannel:
        try:
            entity = await client.get_entity(channel)
            kwargs: dict = {"limit": limit}
            if min_id:
                kwargs.update(min_id=min_id, reverse=True)
            elif offset_date is not None:
                kwargs.update(offset_date=offset_date, reverse=True)
            messages = [msg async for msg in client.iter_messages(entity, **kwargs)]
        except Exception as e:
            raise _translate(e) from e
        return MtChannel(
            title=getattr(entity, "title", "") or "", posts=posts_from_messages(messages)
        )

    def close(self) -> None:
        with self._lock:
            client, loop, thread = self._client, self._loop, self._thread
            self._client = self._loop = self._thread = None
        if client is not None and loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(client.disconnect(), loop).result(10)
            except Exception as e:  # a broken connection must not fail the collect run
                log.debug("MTProto disconnect: %s", e)
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
        if loop is not None:
            loop.close()


def reader_factory(
    credentials: TelegramCredentials, tg: TelegramConfig
) -> Callable[[], MtprotoReader]:
    """A no-argument factory so nothing connects until a telegram source is polled."""

    def build() -> MtprotoReader:
        return TelethonReader(
            credentials,
            flood_threshold=tg.flood_sleep_threshold,
            request_timeout=tg.request_timeout,
            concurrency=tg.concurrency,
        )

    return build


# ── CLI helpers ────────────────────────────────────────────────────────────


def _account_name(me) -> str:
    parts = [getattr(me, "first_name", "") or "", getattr(me, "last_name", "") or ""]
    name = " ".join(p for p in parts if p).strip()
    username = getattr(me, "username", "") or ""
    if username:
        name = f"{name} (@{username})".strip()
    return name or str(getattr(me, "id", ""))


def login(
    credentials: TelegramCredentials,
    *,
    phone: str = "",
    prompt: Callable[[str], str] = input,
    secret_prompt: Callable[[str], str] = getpass.getpass,
) -> str:
    """Interactive sign-in; writes the session file and returns the account name."""
    return asyncio.run(_login(credentials, phone, prompt, secret_prompt))


async def _login(credentials: TelegramCredentials, phone, prompt, secret_prompt) -> str:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    os.makedirs(os.path.dirname(credentials.session_path) or ".", exist_ok=True)
    client = TelegramClient(
        credentials.session_path,
        credentials.api_id,
        credentials.api_hash,
        device_model=DEVICE_MODEL,
        receive_updates=False,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            phone = (phone or prompt("Телефон (в формате +79991234567): ")).strip()
            if not phone:
                raise MtprotoError("phone number is required")
            await client.send_code_request(phone)
            code = prompt("Код из Telegram: ").strip()
            try:
                await client.sign_in(phone=phone, code=code)
            except SessionPasswordNeededError:
                await client.sign_in(password=secret_prompt("Пароль двухфакторной аутентификации: "))
        return _account_name(await client.get_me())
    except Exception as e:
        raise _translate(e) from e
    finally:
        await client.disconnect()


def session_status(credentials: TelegramCredentials) -> dict:
    """Connect read-only and report whether the stored session can be used."""
    return asyncio.run(_session_status(credentials))


async def _session_status(credentials: TelegramCredentials) -> dict:
    from telethon import TelegramClient

    status = {
        "session_path": credentials.session_path,
        "session_exists": os.path.exists(credentials.session_path),
        "authorized": False,
        "account": "",
    }
    if not status["session_exists"]:
        return status
    client = TelegramClient(
        credentials.session_path,
        credentials.api_id,
        credentials.api_hash,
        device_model=DEVICE_MODEL,
        receive_updates=False,
    )
    try:
        await client.connect()
        status["authorized"] = await client.is_user_authorized()
        if status["authorized"]:
            status["account"] = _account_name(await client.get_me())
    except Exception as e:
        raise _translate(e) from e
    finally:
        await client.disconnect()
    return status


def delete_session(credentials: TelegramCredentials) -> list[str]:
    """Remove the session file and its SQLite journal; returns what was deleted."""
    removed = []
    for path in (credentials.session_path, f"{credentials.session_path}-journal"):
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    return removed
