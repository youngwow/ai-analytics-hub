"""Typed configuration loaded from config.yaml.

`Config.load()` parses and validates; services receive the slice they need
(e.g. `Collector(config, ...)` reads `config.scraper`) instead of importing a
global, which keeps them trivially testable via `Config.from_dict(...)`.
"""

from __future__ import annotations

from dataclasses import MISSING, dataclass, field

import yaml

from .paths import DEFAULT_PATHS


class ConfigError(Exception):
    """Raised when config.yaml is missing required keys or is malformed."""


@dataclass(frozen=True)
class ScraperConfig:
    date_window_hours: float
    request_timeout: float
    max_redirects: int
    user_agent: str
    accept_language: str
    concurrency: int
    per_host_concurrency: int
    fetch_fulltext: bool
    fulltext_timeout: float
    fulltext_max_chars: int
    max_new_per_source: int
    store_raw_html: bool

    @property
    def headers(self) -> dict[str, str]:
        """Default request headers: a browser-like UA and Russian language preference."""
        return {"User-Agent": self.user_agent, "Accept-Language": self.accept_language}


@dataclass(frozen=True)
class TelegramConfig:
    backfill_pages: int


@dataclass(frozen=True)
class SitemapConfig:
    max_sitemaps: int
    max_urls: int


@dataclass(frozen=True)
class TavilyConfig:
    api_key_env: str
    max_results: int
    search_depth: str
    days: int = 7  # default recency window for `search` (--days)
    country: str = ""  # boost results from this country for topic=general; "" → not sent
    language: str = ""  # ISO 639-1; boosts hits and steers the answer's language; "" → not sent


_SECTIONS = {
    "scraper": ScraperConfig,
    "telegram": TelegramConfig,
    "sitemap": SitemapConfig,
    "tavily": TavilyConfig,
}


def _build_section(name: str, cls: type, raw: dict):
    section = raw.get(name)
    if not isinstance(section, dict):
        raise ConfigError(f"config.yaml: missing or non-mapping section '{name}'")
    fields = cls.__dataclass_fields__
    missing = [k for k, f in fields.items() if k not in section and f.default is MISSING]
    if missing:
        raise ConfigError(f"config.yaml: section '{name}' missing keys: {missing}")
    try:
        return cls(**{k: section[k] for k in fields if k in section})
    except TypeError as e:
        raise ConfigError(f"config.yaml: section '{name}' is malformed: {e}") from e


@dataclass(frozen=True)
class Config:
    scraper: ScraperConfig
    telegram: TelegramConfig
    sitemap: SitemapConfig
    tavily: TavilyConfig
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        if not isinstance(raw, dict):
            raise ConfigError("config.yaml: top level must be a mapping")
        sections = {name: _build_section(name, cls_, raw) for name, cls_ in _SECTIONS.items()}
        cfg = cls(raw=raw, **sections)
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or DEFAULT_PATHS.config_path
        try:
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except FileNotFoundError as e:
            raise ConfigError(f"config.yaml not found at {path}") from e
        return cls.from_dict(raw)

    def validate(self) -> None:
        s = self.scraper
        if s.concurrency < 1 or s.per_host_concurrency < 1:
            raise ConfigError(
                "config.yaml: scraper.concurrency and per_host_concurrency must be >= 1"
            )
        if s.date_window_hours <= 0:
            raise ConfigError("config.yaml: scraper.date_window_hours must be > 0")
        if self.tavily.search_depth not in ("basic", "fast", "advanced", "ultra-fast"):
            raise ConfigError(
                f"config.yaml: unknown tavily.search_depth '{self.tavily.search_depth}'"
            )
        if self.tavily.days < 1:
            raise ConfigError("config.yaml: tavily.days must be >= 1")
