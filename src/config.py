"""Configuration: the YAML `Config` for the domain and the env-backed `Settings` for the process.

`Config.load()` parses and validates config.yaml; services receive the slice they
need (e.g. `Collector(config, ...)` reads `config.scraper`) instead of importing a
global, which keeps them trivially testable via `Config.from_dict(...)`.

`Settings` (pydantic-settings) holds only deployment knobs — host, port, CORS,
log level, the data root — read from the environment and `<HUB_ROOT>/.env`.
Secrets keep their YAML-configurable variable names and are read through
`utils.load_env_secret` from the same `.env`.
"""

from __future__ import annotations

import os
from dataclasses import MISSING, dataclass, field
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .paths import DEFAULT_PATHS, DEFAULT_ROOT, ProjectPaths


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


MTPROTO_MODES = ("auto", "off", "only")
# "on" and friends are what YAML turns a bare `on:` into; treat them as `auto`.
_MTPROTO_SYNONYMS = {"on": "auto", "true": "auto", "yes": "auto", "false": "off", "no": "off"}


@dataclass(frozen=True)
class TelegramConfig:
    backfill_pages: int
    mtproto: str = "auto"  # auto: MTProto when a session exists, else the t.me/s/ preview
    api_id_env: str = "TELEGRAM_API_ID"
    api_hash_env: str = "TELEGRAM_API_HASH"
    session_name: str = "telegram"  # data/<session_name>.session
    max_posts: int = 100  # posts per channel per run over MTProto
    backfill_posts: int = 300  # posts per channel with --backfill
    concurrency: int = 2  # parallel MTProto history requests
    flood_sleep_threshold: float = 60.0  # longer FloodWait fails the source instead of waiting
    request_timeout: float = 60.0  # seconds for one channel's history

    def __post_init__(self):
        # YAML 1.1 reads a bare `off` / `on` / `no` / `yes` as a boolean, and
        # `mtproto: off` is exactly what a reader would write. Accept every
        # spelling of those two, quoted or not, so the mode never depends on
        # whether the value happened to hit a YAML keyword.
        mode = self.mtproto
        if isinstance(mode, bool):
            mode = "on" if mode else "off"
        mode = str(mode).strip().lower()
        object.__setattr__(self, "mtproto", _MTPROTO_SYNONYMS.get(mode, mode))


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


@dataclass(frozen=True)
class LLMConfig:
    """Провайдер модели. По умолчанию GLM-5.3 в Ollama Cloud (см. research.md, R-01)."""

    host: str = "https://ollama.com"
    model: str = "glm-5.3-flash"
    api_key_env: str = "OLLAMA_API_KEY"
    embed_model: str = "embeddinggemma"
    embed_dimensions: int = 768
    request_timeout: float = 90.0
    max_retries: int = 2
    retry_backoff: float = 2.0
    temperature: float = 0.2
    # Tri-state on purpose: None means "do not send the parameter". A reasoning
    # model then keeps its chain of thought in `message.thinking` and leaves
    # `content` clean; think=False makes it reason inside `content` instead.
    think: bool | None = None
    max_output_tokens: int = -1  # -1: no cap — a truncated answer is a wasted call


@dataclass(frozen=True)
class ProcessingConfig:
    """Пайплайн 1.2: окна, пороги и бюджеты обработки."""

    concurrency: int = 2
    max_new_per_run: int = 200
    max_chars: int = 12000
    chunk_chars: int = 6000
    candidate_window_days: int = 7
    simhash_distance: int = 8  # правки-перепечатки дают 2-4; разные документы — от 18
    cosine_threshold: float = 0.86
    borderline_low: float = 0.35
    borderline_high: float = 0.5
    # Очередь обработки: НПА от регулятора не должен ждать за лентой СМИ, когда
    # очередь длиннее одного прогона. Ключ — `sources.category`.
    category_weights: dict = field(
        default_factory=lambda: {"regulator": 3, "telegram": 2, "media": 1, "manual": 1}
    )


EMBEDDING_PROVIDERS = ("local", "ollama", "off")
EMBEDDING_DEVICES = ("auto", "mps", "cuda", "cpu")
EMBEDDING_DTYPES = ("auto", "bfloat16", "float16", "float32")
REDUCTIONS = ("none", "pca", "umap")
CLUSTER_SELECTION = ("eom", "leaf")


@dataclass(frozen=True)
class EmbeddingsConfig:
    """Откуда берутся эмбеддинги для S1 и для кластеризации карточек.

    `local` — модель sentence-transformers на этой машине (по умолчанию
    `ai-sage/Giga-Embeddings-instruct`, 2048 измерений); `ollama` — прежний
    облачный путь через `llm.host` и `llm.embed_model` (нужен ключ); `off` —
    эмбеддингов нет, S1 работает только по URL и SimHash и говорит об этом в логе.
    """

    provider: str = "local"
    model: str = "ai-sage/Giga-Embeddings-instruct"
    device: str = "auto"  # auto: cuda → mps → cpu
    dtype: str = "auto"  # auto: bfloat16 на cuda/mps, float32 на cpu
    batch_size: int = 16
    max_seq_length: int = 1024  # токенов на текст; S1 и так режет текст до 2000 символов
    dimensions: int = 2048  # ожидаемая длина вектора; 0 — не проверять
    # Инструкция instruct-модели, одна для обеих сторон сравнения. На парах
    # «дубль / не дубль» она раздвигает косинусы: дубли ≥ 0.91, не-дубли ≤ 0.80,
    # и порог S1 0.86 попадает в середину зазора; без неё дубли начинаются с 0.795
    # и половина парафразов до 0.86 не дотягивает. Пусто — режим документа.
    prompt: str = "Instruct: Retrieve news that report the same event\nQuery: "

    @property
    def enabled(self) -> bool:
        return self.provider != "off"


@dataclass(frozen=True)
class ClusteringConfig:
    """Вторая дедупликация: HDBSCAN по эмбеддингам саммари после прогона.

    Находит группы уже созданных карточек-дублей без фиксированного порога
    косинуса. Кластер — это предложение «вероятный дубль», а не автосклейка:
    объединение подтверждает аналитик. НПА не кластеризуются никогда.
    """

    enabled: bool = True
    min_cluster_size: int = 2  # группа дублей — это уже пара; калибровка на золотом наборе
    min_samples: int = 1  # плотность: чем больше, тем консервативнее (больше шума)
    cluster_selection_method: str = "eom"  # eom: устойчивые группы, leaf: самые мелкие
    cluster_selection_epsilon: float = 0.0  # 0 — без склейки соседних кластеров
    # Нижний край зоны «вероятный дубль»: внутри кластера HDBSCAN остаются только
    # связные компоненты по косинусу ≥ этого. Сам HDBSCAN считает кластером любую
    # пару взаимных ближайших соседей (плотность у него относительная) и тянет в
    # плотную группу всё, что «уронил» по дороге к её ядру. Верхний край зоны —
    # `processing.cosine_threshold`: там S1 склеивает сам, без вопроса.
    min_similarity: float = 0.7
    reduction: str = "pca"  # none | pca | umap (umap — необязательная зависимость)
    n_components: int = 32  # размерность после снижения; 0 — без снижения
    window_days: int = 7  # свежие карточки сравниваются с карточками этого окна
    max_pool: int = 500  # не больше стольких карточек окна за один прогон
    seed: int = 0  # детерминизм UMAP/PCA


@dataclass(frozen=True)
class ApiConfig:
    """Доменные настройки HTTP-слоя. Адрес, порт и /docs — в `Settings` (окружение)."""

    timezone: str = "Europe/Moscow"  # чьи это сутки, когда фильтр получил голую дату


_SECTIONS = {
    "scraper": ScraperConfig,
    "telegram": TelegramConfig,
    "sitemap": SitemapConfig,
    "tavily": TavilyConfig,
    "llm": LLMConfig,
    "processing": ProcessingConfig,
    "embeddings": EmbeddingsConfig,
    "clustering": ClusteringConfig,
    "api": ApiConfig,
}


def _has_default(field_info) -> bool:
    """У поля есть значение по умолчанию — прямое или через фабрику."""
    return field_info.default is not MISSING or field_info.default_factory is not MISSING


def _build_section(name: str, cls: type, raw: dict):
    fields = cls.__dataclass_fields__
    section = raw.get(name)
    if section is None and all(_has_default(f) for f in fields.values()):
        # Секция, у которой все поля со значениями по умолчанию, необязательна:
        # старый config.yaml должен грузиться после добавления новой секции.
        section = {}
    if not isinstance(section, dict):
        raise ConfigError(f"config.yaml: missing or non-mapping section '{name}'")
    missing = [k for k, f in fields.items() if k not in section and not _has_default(f)]
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
    llm: LLMConfig = field(default_factory=LLMConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)
    clustering: ClusteringConfig = field(default_factory=ClusteringConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
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
        tg = self.telegram
        if tg.mtproto not in MTPROTO_MODES:
            raise ConfigError(
                f"config.yaml: telegram.mtproto must be one of {list(MTPROTO_MODES)}, "
                f"got '{tg.mtproto}'"
            )
        if tg.max_posts < 1 or tg.backfill_posts < 1 or tg.concurrency < 1:
            raise ConfigError(
                "config.yaml: telegram.max_posts, backfill_posts and concurrency must be >= 1"
            )
        if tg.request_timeout <= 0 or tg.flood_sleep_threshold < 0:
            raise ConfigError(
                "config.yaml: telegram.request_timeout must be > 0 and "
                "flood_sleep_threshold >= 0"
            )
        llm = self.llm
        if not llm.model.strip() or not llm.host.strip():
            raise ConfigError("config.yaml: llm.model and llm.host must be non-empty")
        if llm.request_timeout <= 0 or llm.max_retries < 0 or llm.retry_backoff < 0:
            raise ConfigError(
                "config.yaml: llm.request_timeout must be > 0, max_retries and "
                "retry_backoff >= 0"
            )
        if llm.embed_dimensions < 1:
            raise ConfigError("config.yaml: llm.embed_dimensions must be >= 1")
        if llm.max_output_tokens == 0 or llm.max_output_tokens < -1:
            raise ConfigError("config.yaml: llm.max_output_tokens must be -1 or a positive number")
        if not 0 <= llm.temperature <= 2:
            raise ConfigError("config.yaml: llm.temperature must be within [0, 2]")
        pr = self.processing
        if pr.concurrency < 1 or pr.max_new_per_run < 1:
            raise ConfigError(
                "config.yaml: processing.concurrency and max_new_per_run must be >= 1"
            )
        if pr.max_chars < 1 or pr.chunk_chars < 1 or pr.chunk_chars > pr.max_chars:
            raise ConfigError(
                "config.yaml: processing.chunk_chars must be within [1, max_chars]"
            )
        if pr.candidate_window_days < 1 or not 0 <= pr.simhash_distance <= 64:
            raise ConfigError(
                "config.yaml: processing.candidate_window_days >= 1 and "
                "simhash_distance within [0, 64]"
            )
        for name in ("cosine_threshold", "borderline_low", "borderline_high"):
            value = getattr(pr, name)
            if not 0 <= value <= 1:
                raise ConfigError(f"config.yaml: processing.{name} must be within [0, 1]")
        if not isinstance(pr.category_weights, dict):
            raise ConfigError("config.yaml: processing.category_weights must be a mapping")
        for category, weight in pr.category_weights.items():
            if not isinstance(weight, int) or weight < 0:
                raise ConfigError(
                    f"config.yaml: processing.category_weights['{category}'] must be an "
                    "integer >= 0"
                )
        if pr.borderline_low > pr.borderline_high:
            raise ConfigError(
                "config.yaml: processing.borderline_low must be <= borderline_high"
            )
        em = self.embeddings
        if em.provider not in EMBEDDING_PROVIDERS:
            raise ConfigError(
                f"config.yaml: embeddings.provider must be one of {list(EMBEDDING_PROVIDERS)}, "
                f"got '{em.provider}'"
            )
        if em.provider == "local" and not str(em.model).strip():
            raise ConfigError("config.yaml: embeddings.model must be non-empty for provider 'local'")
        if em.device not in EMBEDDING_DEVICES:
            raise ConfigError(
                f"config.yaml: embeddings.device must be one of {list(EMBEDDING_DEVICES)}, "
                f"got '{em.device}'"
            )
        if em.dtype not in EMBEDDING_DTYPES:
            raise ConfigError(
                f"config.yaml: embeddings.dtype must be one of {list(EMBEDDING_DTYPES)}, "
                f"got '{em.dtype}'"
            )
        if em.batch_size < 1 or em.max_seq_length < 1 or em.dimensions < 0:
            raise ConfigError(
                "config.yaml: embeddings.batch_size and max_seq_length must be >= 1, "
                "dimensions >= 0"
            )
        cl = self.clustering
        if cl.min_cluster_size < 2 or cl.min_samples < 1:
            raise ConfigError(
                "config.yaml: clustering.min_cluster_size must be >= 2 and min_samples >= 1"
            )
        if cl.cluster_selection_method not in CLUSTER_SELECTION:
            raise ConfigError(
                "config.yaml: clustering.cluster_selection_method must be one of "
                f"{list(CLUSTER_SELECTION)}, got '{cl.cluster_selection_method}'"
            )
        if cl.cluster_selection_epsilon < 0:
            raise ConfigError("config.yaml: clustering.cluster_selection_epsilon must be >= 0")
        if not 0 <= cl.min_similarity <= 1:
            raise ConfigError("config.yaml: clustering.min_similarity must be within [0, 1]")
        if cl.reduction not in REDUCTIONS:
            raise ConfigError(
                f"config.yaml: clustering.reduction must be one of {list(REDUCTIONS)}, "
                f"got '{cl.reduction}'"
            )
        if cl.n_components < 0 or cl.window_days < 1 or cl.max_pool < 2:
            raise ConfigError(
                "config.yaml: clustering.n_components >= 0, window_days >= 1 and max_pool >= 2"
            )
        try:
            ZoneInfo(self.api.timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ConfigError(f"config.yaml: unknown api.timezone '{self.api.timezone}'") from e


# ── process settings (environment / .env) ──────────────────────────────────


class Settings(BaseSettings):
    """Параметры процесса. Только это читает окружение; остальное — `Config`."""

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",  # сам файл выбирает get_settings(): он зависит от HUB_ROOT
        case_sensitive=False,
        extra="ignore",  # в реальном .env лежат ключи, которые здесь не моделируются
    )

    app_name: str = "ai-analytics-hub"
    environment: Literal["local", "dev", "prod"] = "local"
    debug: bool = False

    api_prefix: str = "/api/v1"
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)
    docs: bool = True  # /docs и /openapi.json
    cors_origins: list[str] = ["*"]  # в окружении — JSON: CORS_ORIGINS='["http://localhost:5173"]'
    log_level: str = "INFO"

    hub_root: str = DEFAULT_ROOT  # всегда задаётся в get_settings(); строка HUB_ROOT= в .env не читается


@lru_cache
def get_settings() -> Settings:
    """Единственное место, где процесс решает, откуда брать `.env`.

    Корень берётся из окружения до создания `Settings`, чтобы тесты на временном
    `HUB_ROOT` никогда не прочитали `.env` разработчика в корне репозитория.
    """
    root = os.environ.get("HUB_ROOT") or DEFAULT_ROOT
    return Settings(_env_file=os.path.join(root, ".env"), hub_root=root)


@lru_cache
def get_paths() -> ProjectPaths:
    return ProjectPaths.from_root(get_settings().hub_root)


@lru_cache
def get_config() -> Config:
    # Путь явно: `Config.load(None)` упал бы на DEFAULT_PATHS, вычисленные при импорте.
    return Config.load(get_paths().config_path)
