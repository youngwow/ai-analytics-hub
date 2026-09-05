"""src/config.py — validation of config.yaml sections."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from src.config import (
    MTPROTO_MODES,
    ApiConfig,
    Config,
    ConfigError,
    LLMConfig,
    ProcessingConfig,
    ScraperConfig,
    Settings,
    TavilyConfig,
    TelegramConfig,
)
from src.paths import DEFAULT_PATHS

# Sections every key of which carries a default — `Config` fills them in itself.
_SECTIONS = {"llm": LLMConfig, "processing": ProcessingConfig, "api": ApiConfig}


def test_from_dict_builds_every_section(raw_config):
    cfg = Config.from_dict(raw_config)
    assert cfg.scraper.date_window_hours == 72
    assert cfg.scraper.concurrency == 4
    assert cfg.telegram.backfill_pages == 1
    assert cfg.sitemap.max_sitemaps == 5
    assert cfg.tavily.api_key_env == "TAVILY_API"
    assert cfg.raw is raw_config


def test_scraper_headers_carry_user_agent_and_language(config):
    assert config.scraper.headers == {"User-Agent": "test-agent", "Accept-Language": "ru"}


def test_scraper_config_is_frozen(config):
    with pytest.raises(AttributeError):
        config.scraper.concurrency = 1  # type: ignore[misc]


@pytest.mark.parametrize("section", ["scraper", "telegram", "sitemap", "tavily"])
def test_missing_required_section_raises(raw_config, section):
    """Only sections with keys that have no default stay mandatory."""
    del raw_config[section]
    with pytest.raises(ConfigError, match=f"missing or non-mapping section '{section}'"):
        Config.from_dict(raw_config)


@pytest.mark.parametrize("section", ["llm", "processing", "api"])
def test_section_whose_keys_all_have_defaults_may_be_absent(raw_config, section):
    """An older config.yaml must keep loading after a new section is introduced."""
    raw_config.pop(section, None)
    assert section not in raw_config
    cfg = Config.from_dict(raw_config)
    assert getattr(cfg, section) == _SECTIONS[section]()


def test_non_mapping_section_raises(raw_config):
    raw_config["telegram"] = "nope"
    with pytest.raises(ConfigError, match="missing or non-mapping section 'telegram'"):
        Config.from_dict(raw_config)


def test_missing_key_raises_and_names_it(raw_config):
    del raw_config["scraper"]["user_agent"]
    with pytest.raises(ConfigError, match=r"section 'scraper' missing keys: \['user_agent'\]"):
        Config.from_dict(raw_config)


def test_top_level_must_be_a_mapping():
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        Config.from_dict(["scraper"])  # type: ignore[arg-type]


@pytest.mark.parametrize("depth", ["deep", "", "BASIC"])
def test_bad_tavily_search_depth_raises(raw_config, depth):
    raw_config["tavily"]["search_depth"] = depth
    with pytest.raises(ConfigError, match="unknown tavily.search_depth"):
        Config.from_dict(raw_config)


@pytest.mark.parametrize("depth", ["basic", "fast", "advanced", "ultra-fast"])
def test_known_tavily_search_depths_accepted(raw_config, depth):
    raw_config["tavily"]["search_depth"] = depth
    assert Config.from_dict(raw_config).tavily.search_depth == depth


def test_tavily_keys_with_defaults_may_be_omitted(raw_config):
    assert set(raw_config["tavily"]) == {"api_key_env", "max_results", "search_depth"}
    assert Config.from_dict(raw_config).tavily == TavilyConfig(
        api_key_env="TAVILY_API",
        max_results=10,
        search_depth="basic",
        days=7,
        country="",
        language="",
    )


def test_tavily_optional_keys_are_read_when_present(raw_config):
    raw_config["tavily"].update(days=3, country="russia", language="ru")
    tavily = Config.from_dict(raw_config).tavily
    assert (tavily.days, tavily.country, tavily.language) == (3, "russia", "ru")


def test_required_tavily_key_is_still_reported_when_missing(raw_config):
    del raw_config["tavily"]["search_depth"]
    with pytest.raises(ConfigError, match=r"section 'tavily' missing keys: \['search_depth'\]"):
        Config.from_dict(raw_config)


def test_unknown_keys_in_a_section_are_ignored(raw_config):
    raw_config["tavily"]["future_option"] = True
    assert Config.from_dict(raw_config).tavily.days == 7


@pytest.mark.parametrize("days", [0, -1])
def test_tavily_days_below_one_raises(raw_config, days):
    raw_config["tavily"]["days"] = days
    with pytest.raises(ConfigError, match="tavily.days must be >= 1"):
        Config.from_dict(raw_config)


def test_tavily_days_of_one_is_accepted(raw_config):
    raw_config["tavily"]["days"] = 1
    assert Config.from_dict(raw_config).tavily.days == 1


def test_repo_config_yaml_loads_with_search_settings():
    cfg = Config.load(DEFAULT_PATHS.config_path)
    assert cfg.tavily.days == 7
    assert (cfg.tavily.country, cfg.tavily.language) == ("russia", "ru")


# ── telegram ───────────────────────────────────────────────────────────────


def test_telegram_keys_with_defaults_may_be_omitted(raw_config):
    assert set(raw_config["telegram"]) == {"backfill_pages"}
    assert Config.from_dict(raw_config).telegram == TelegramConfig(
        backfill_pages=1,
        mtproto="auto",
        api_id_env="TELEGRAM_API_ID",
        api_hash_env="TELEGRAM_API_HASH",
        session_name="telegram",
        max_posts=100,
        backfill_posts=300,
        concurrency=2,
        flood_sleep_threshold=60.0,
        request_timeout=60.0,
    )


def test_telegram_optional_keys_are_read_when_present(raw_config):
    raw_config["telegram"].update(
        mtproto="only", api_id_env="TG_ID", api_hash_env="TG_HASH", session_name="work",
        max_posts=40, backfill_posts=90, concurrency=1, flood_sleep_threshold=0,
        request_timeout=15,
    )
    tg = Config.from_dict(raw_config).telegram
    assert (tg.mtproto, tg.api_id_env, tg.api_hash_env, tg.session_name) == (
        "only", "TG_ID", "TG_HASH", "work"
    )
    assert (tg.max_posts, tg.backfill_posts, tg.concurrency) == (40, 90, 1)
    assert (tg.flood_sleep_threshold, tg.request_timeout) == (0, 15)


def test_required_telegram_key_is_still_reported_when_missing(raw_config):
    del raw_config["telegram"]["backfill_pages"]
    with pytest.raises(ConfigError, match=r"section 'telegram' missing keys: \['backfill_pages'\]"):
        Config.from_dict(raw_config)


@pytest.mark.parametrize("mode", MTPROTO_MODES)
def test_known_mtproto_modes_are_accepted(raw_config, mode):
    raw_config["telegram"]["mtproto"] = mode
    assert Config.from_dict(raw_config).telegram.mtproto == mode


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("auto", "auto"),
        ("AUTO", "auto"),
        ("Auto", "auto"),
        (" only ", "only"),
        ("OFF", "off"),
        (True, "auto"),  # YAML 1.1 turns a bare `on` / `yes` into the boolean True
        (False, "off"),  # …and a bare `off` / `no` into False
        ("on", "auto"),  # the same words quoted must land in the same place
        ("yes", "auto"),
        ("true", "auto"),
        (" YES ", "auto"),
        ("no", "off"),
        ("false", "off"),
    ],
    ids=["plain", "upper", "capitalised", "padded", "upper-off", "true", "false", "quoted-on",
         "quoted-yes", "quoted-true", "padded-upper-yes", "quoted-no", "quoted-false"],
)
def test_mtproto_spellings_are_normalised(raw_config, mode, expected):
    raw_config["telegram"]["mtproto"] = mode
    assert Config.from_dict(raw_config).telegram.mtproto == expected


def test_a_bare_off_in_yaml_is_a_boolean_and_still_means_off(raw_config):
    """The trap `__post_init__` exists for: README says `mtproto: off`, YAML says False."""
    section = yaml.safe_load("telegram:\n  backfill_pages: 1\n  mtproto: off\n")["telegram"]
    assert section["mtproto"] is False
    raw_config["telegram"] = section
    assert Config.from_dict(raw_config).telegram.mtproto == "off"


def test_quoting_a_yaml_keyword_does_not_change_the_mode(raw_config):
    """The mode must not depend on whether the value hit a YAML 1.1 keyword."""
    loaded = yaml.safe_load('bare:\n  mtproto: on\nquoted:\n  mtproto: "on"\n')
    assert loaded["bare"]["mtproto"] is True
    assert loaded["quoted"]["mtproto"] == "on"

    modes = []
    for key in ("bare", "quoted"):
        raw_config["telegram"]["mtproto"] = loaded[key]["mtproto"]
        modes.append(Config.from_dict(raw_config).telegram.mtproto)
    assert modes == ["auto", "auto"]


@pytest.mark.parametrize(
    "mode", ["telethon", "", "   ", "web", "mtproto", "enabled", None, 0, 1, ["off"]],
    ids=["unknown", "empty", "blank", "web", "section-name", "enabled", "none", "zero", "one",
         "list"],
)
def test_unknown_mtproto_mode_raises(raw_config, mode):
    raw_config["telegram"]["mtproto"] = mode
    with pytest.raises(ConfigError, match=r"telegram.mtproto must be one of"):
        Config.from_dict(raw_config)


@pytest.mark.parametrize("key", ["max_posts", "backfill_posts", "concurrency"])
@pytest.mark.parametrize("value", [0, -1])
def test_telegram_counts_below_one_raise(raw_config, key, value):
    raw_config["telegram"][key] = value
    with pytest.raises(
        ConfigError,
        match="telegram.max_posts, backfill_posts and concurrency must be >= 1",
    ):
        Config.from_dict(raw_config)


@pytest.mark.parametrize("key", ["max_posts", "backfill_posts", "concurrency"])
def test_telegram_counts_of_one_are_accepted(raw_config, key):
    raw_config["telegram"][key] = 1
    assert getattr(Config.from_dict(raw_config).telegram, key) == 1


@pytest.mark.parametrize(
    "overrides",
    [{"request_timeout": 0}, {"request_timeout": -5}, {"flood_sleep_threshold": -1}],
    ids=["zero-timeout", "negative-timeout", "negative-flood-threshold"],
)
def test_telegram_timeouts_out_of_range_raise(raw_config, overrides):
    raw_config["telegram"].update(overrides)
    with pytest.raises(
        ConfigError,
        match="telegram.request_timeout must be > 0 and flood_sleep_threshold >= 0",
    ):
        Config.from_dict(raw_config)


def test_zero_flood_sleep_threshold_is_accepted(raw_config):
    raw_config["telegram"]["flood_sleep_threshold"] = 0
    assert Config.from_dict(raw_config).telegram.flood_sleep_threshold == 0


def test_repo_config_yaml_loads_the_telegram_section():
    tg = Config.load(DEFAULT_PATHS.config_path).telegram
    assert tg.mtproto == "auto"
    assert (tg.api_id_env, tg.api_hash_env, tg.session_name) == (
        "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "telegram"
    )
    assert (tg.max_posts, tg.backfill_posts, tg.concurrency) == (100, 300, 2)
    assert (tg.flood_sleep_threshold, tg.request_timeout) == (60, 60)


@pytest.mark.parametrize("key", ["concurrency", "per_host_concurrency"])
def test_concurrency_below_one_raises(raw_config, key):
    raw_config["scraper"][key] = 0
    with pytest.raises(ConfigError, match="must be >= 1"):
        Config.from_dict(raw_config)


def test_non_positive_window_raises(raw_config):
    raw_config["scraper"]["date_window_hours"] = 0
    with pytest.raises(ConfigError, match="date_window_hours must be > 0"):
        Config.from_dict(raw_config)


def test_load_reads_yaml_file(tmp_path, raw_config):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw_config, allow_unicode=True), encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.scraper == ScraperConfig(**raw_config["scraper"])


def test_load_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="config.yaml not found"):
        Config.load(str(tmp_path / "nope.yaml"))


# ── llm (task 1.2) ─────────────────────────────────────────────────────────


def test_llm_section_may_be_empty_and_falls_back_to_defaults(raw_config):
    raw_config["llm"] = {}
    assert Config.from_dict(raw_config).llm == LLMConfig(
        host="https://ollama.com",
        model="glm-5.3-flash",
        api_key_env="OLLAMA_API_KEY",
        embed_model="embeddinggemma",
        embed_dimensions=768,
        request_timeout=90.0,
        max_retries=2,
        retry_backoff=2.0,
        temperature=0.2,
    )


def test_llm_keys_are_read_when_present(raw_config):
    raw_config["llm"] = {
        "host": "http://localhost:11434",
        "model": "qwen3:8b",
        "api_key_env": "LOCAL_KEY",
        "embed_model": "bge-m3",
        "embed_dimensions": 1024,
        "request_timeout": 30,
        "max_retries": 0,
        "retry_backoff": 0,
        "temperature": 0,
    }
    llm = Config.from_dict(raw_config).llm
    assert (llm.host, llm.model, llm.api_key_env) == ("http://localhost:11434", "qwen3:8b", "LOCAL_KEY")
    assert (llm.embed_model, llm.embed_dimensions) == ("bge-m3", 1024)
    assert (llm.request_timeout, llm.max_retries, llm.retry_backoff) == (30, 0, 0)
    assert llm.temperature == 0


@pytest.mark.parametrize(
    "overrides",
    [{"model": ""}, {"model": "   "}, {"host": ""}, {"host": "\t"}],
    ids=["empty-model", "blank-model", "empty-host", "blank-host"],
)
def test_empty_llm_model_or_host_raises(raw_config, overrides):
    raw_config["llm"].update(overrides)
    with pytest.raises(ConfigError, match=r"llm.model and llm.host must be non-empty"):
        Config.from_dict(raw_config)


@pytest.mark.parametrize(
    "overrides",
    [{"request_timeout": 0}, {"request_timeout": -1}, {"max_retries": -1}, {"retry_backoff": -0.5}],
    ids=["zero-timeout", "negative-timeout", "negative-retries", "negative-backoff"],
)
def test_llm_timeout_and_retry_bounds(raw_config, overrides):
    raw_config["llm"].update(overrides)
    with pytest.raises(ConfigError, match=r"llm.request_timeout must be > 0"):
        Config.from_dict(raw_config)


def test_zero_retries_and_zero_backoff_are_accepted(raw_config):
    raw_config["llm"].update(max_retries=0, retry_backoff=0)
    llm = Config.from_dict(raw_config).llm
    assert (llm.max_retries, llm.retry_backoff) == (0, 0)


@pytest.mark.parametrize("value", [0, -1, -768], ids=["zero", "negative", "negative-default"])
def test_embed_dimensions_below_one_raises(raw_config, value):
    raw_config["llm"]["embed_dimensions"] = value
    with pytest.raises(ConfigError, match=r"llm.embed_dimensions must be >= 1"):
        Config.from_dict(raw_config)


@pytest.mark.parametrize("value", [-0.1, 2.1, 3], ids=["below-zero", "just-above-two", "three"])
def test_temperature_outside_zero_to_two_raises(raw_config, value):
    raw_config["llm"]["temperature"] = value
    with pytest.raises(ConfigError, match=r"llm.temperature must be within \[0, 2\]"):
        Config.from_dict(raw_config)


@pytest.mark.parametrize("value", [0, 0.2, 1, 2], ids=["zero", "default", "one", "two"])
def test_temperature_inside_the_range_is_accepted(raw_config, value):
    raw_config["llm"]["temperature"] = value
    assert Config.from_dict(raw_config).llm.temperature == value


# ── processing (task 1.2) ──────────────────────────────────────────────────


def test_processing_section_may_be_empty_and_falls_back_to_defaults(raw_config):
    raw_config["processing"] = {}
    assert Config.from_dict(raw_config).processing == ProcessingConfig(
        concurrency=2,
        max_new_per_run=200,
        max_chars=12000,
        chunk_chars=6000,
        candidate_window_days=7,
        simhash_distance=8,
        cosine_threshold=0.86,
        borderline_low=0.35,
        borderline_high=0.5,
    )


def test_processing_keys_are_read_when_present(raw_config):
    raw_config["processing"] = {
        "concurrency": 4,
        "max_new_per_run": 10,
        "max_chars": 4000,
        "chunk_chars": 4000,
        "candidate_window_days": 1,
        "simhash_distance": 0,
        "cosine_threshold": 0,
        "borderline_low": 0.1,
        "borderline_high": 0.9,
    }
    pr = Config.from_dict(raw_config).processing
    assert (pr.concurrency, pr.max_new_per_run) == (4, 10)
    assert (pr.max_chars, pr.chunk_chars) == (4000, 4000)
    assert (pr.candidate_window_days, pr.simhash_distance) == (1, 0)
    assert pr.cosine_threshold == 0
    assert (pr.borderline_low, pr.borderline_high) == (0.1, 0.9)


@pytest.mark.parametrize("key", ["concurrency", "max_new_per_run"])
@pytest.mark.parametrize("value", [0, -1])
def test_processing_counts_below_one_raise(raw_config, key, value):
    raw_config["processing"][key] = value
    with pytest.raises(
        ConfigError, match="processing.concurrency and max_new_per_run must be >= 1"
    ):
        Config.from_dict(raw_config)


@pytest.mark.parametrize(
    "overrides",
    [
        {"chunk_chars": 12001},
        {"max_chars": 1000, "chunk_chars": 2000},
        {"chunk_chars": 0},
        {"max_chars": 0, "chunk_chars": 0},
    ],
    ids=["chunk-above-max", "chunk-above-smaller-max", "zero-chunk", "zero-max"],
)
def test_chunk_chars_must_fit_inside_max_chars(raw_config, overrides):
    raw_config["processing"].update(overrides)
    with pytest.raises(
        ConfigError, match=r"processing.chunk_chars must be within \[1, max_chars\]"
    ):
        Config.from_dict(raw_config)


def test_chunk_chars_equal_to_max_chars_is_accepted(raw_config):
    raw_config["processing"].update(max_chars=5000, chunk_chars=5000)
    assert Config.from_dict(raw_config).processing.chunk_chars == 5000


@pytest.mark.parametrize(
    "overrides",
    [{"candidate_window_days": 0}, {"simhash_distance": -1}, {"simhash_distance": 65}],
    ids=["zero-window", "negative-distance", "distance-above-64"],
)
def test_window_and_simhash_distance_bounds(raw_config, overrides):
    raw_config["processing"].update(overrides)
    with pytest.raises(ConfigError, match="processing.candidate_window_days >= 1"):
        Config.from_dict(raw_config)


@pytest.mark.parametrize("name", ["cosine_threshold", "borderline_low", "borderline_high"])
@pytest.mark.parametrize("value", [-0.01, 1.01], ids=["below-zero", "above-one"])
def test_processing_ratios_outside_zero_to_one_raise(raw_config, name, value):
    raw_config["processing"][name] = value
    with pytest.raises(ConfigError, match=rf"processing.{name} must be within \[0, 1\]"):
        Config.from_dict(raw_config)


def test_borderline_low_above_high_raises(raw_config):
    raw_config["processing"].update(borderline_low=0.6, borderline_high=0.5)
    with pytest.raises(ConfigError, match="borderline_low must be <= borderline_high"):
        Config.from_dict(raw_config)


def test_equal_borderline_bounds_are_accepted(raw_config):
    raw_config["processing"].update(borderline_low=0.5, borderline_high=0.5)
    pr = Config.from_dict(raw_config).processing
    assert (pr.borderline_low, pr.borderline_high) == (0.5, 0.5)


def test_repo_config_yaml_loads_the_llm_and_processing_sections():
    cfg = Config.load(DEFAULT_PATHS.config_path)
    assert (cfg.llm.host, cfg.llm.model) == ("https://ollama.com", "glm-5.3-flash")
    assert cfg.llm.api_key_env == "OLLAMA_API_KEY"
    assert (cfg.llm.embed_model, cfg.llm.embed_dimensions) == ("embeddinggemma", 768)
    assert (cfg.llm.request_timeout, cfg.llm.max_retries, cfg.llm.temperature) == (300, 2, 0.2)
    assert (cfg.processing.max_chars, cfg.processing.chunk_chars) == (12000, 6000)
    assert cfg.processing.candidate_window_days == 7
    assert cfg.processing.simhash_distance == 8
    assert cfg.processing.cosine_threshold == 0.86
    assert (cfg.processing.borderline_low, cfg.processing.borderline_high) == (0.35, 0.5)


# ── api (task 1.4): only the domain knob stays in YAML ─────────────────────


def test_api_section_falls_back_to_defaults_when_absent(raw_config):
    assert "api" not in raw_config
    assert Config.from_dict(raw_config).api == ApiConfig(timezone="Europe/Moscow")


def test_api_timezone_is_read_when_present(raw_config):
    raw_config["api"] = {"timezone": "Asia/Novosibirsk"}
    assert Config.from_dict(raw_config).api.timezone == "Asia/Novosibirsk"


@pytest.mark.parametrize("zone", ["Mars/Olympus", ""], ids=["unknown", "empty"])
def test_an_unknown_api_timezone_raises(raw_config, zone):
    raw_config["api"] = {"timezone": zone}
    with pytest.raises(ConfigError, match="unknown api.timezone"):
        Config.from_dict(raw_config)


def test_legacy_host_port_and_docs_keys_in_the_api_section_are_ignored(raw_config):
    """An older config.yaml still loads: deployment knobs moved to `Settings`."""
    raw_config["api"] = {"host": "0.0.0.0", "port": 9001, "docs": False}

    cfg = Config.from_dict(raw_config)

    assert cfg.api == ApiConfig(timezone="Europe/Moscow")
    assert not hasattr(cfg.api, "port")


def test_repo_config_yaml_loads_the_api_section():
    cfg = Config.load(DEFAULT_PATHS.config_path)
    assert cfg.api == ApiConfig(timezone="Europe/Moscow")
    assert set(cfg.raw["api"]) == {"timezone"}
    assert (cfg.processing.borderline_low, cfg.processing.borderline_high) == (0.35, 0.5)


# ── Settings: the process knobs that left config.yaml ──────────────────────


def test_settings_defaults_match_what_the_api_section_used_to_carry():
    settings = Settings(_env_file=None)
    assert (settings.host, settings.port, settings.docs) == ("127.0.0.1", 8000, True)
    assert (settings.api_prefix, settings.environment, settings.debug) == ("/api/v1", "local", False)
    assert settings.cors_origins == ["*"]


@pytest.mark.parametrize("port", [0, -1, 65536], ids=["zero", "negative", "above-range"])
def test_settings_port_outside_the_valid_range_raises(port):
    with pytest.raises(ValidationError, match="port"):
        Settings(_env_file=None, port=port)


def test_settings_environment_is_one_of_three_names():
    with pytest.raises(ValidationError, match="environment"):
        Settings(_env_file=None, environment="staging")


def test_settings_ignore_the_secrets_that_live_in_the_same_env_file(tmp_path):
    """`.env` also holds API keys; they are read by `load_env_secret`, not modelled here."""
    env_file = tmp_path / ".env"
    env_file.write_text("OLLAMA_API_KEY=secret\nPORT=8123\n", encoding="utf-8")

    settings = Settings(_env_file=str(env_file))

    assert settings.port == 8123
    assert not hasattr(settings, "ollama_api_key")
