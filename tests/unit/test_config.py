"""src/config.py — validation of config.yaml sections."""

from __future__ import annotations

import pytest
import yaml

from src.config import (
    MTPROTO_MODES,
    Config,
    ConfigError,
    ScraperConfig,
    TavilyConfig,
    TelegramConfig,
)
from src.paths import DEFAULT_PATHS


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
def test_missing_section_raises(raw_config, section):
    del raw_config[section]
    with pytest.raises(ConfigError, match=f"missing or non-mapping section '{section}'"):
        Config.from_dict(raw_config)


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
