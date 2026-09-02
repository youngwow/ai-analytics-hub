"""src/config.py — validation of config.yaml sections."""

from __future__ import annotations

import pytest
import yaml

from src.config import Config, ConfigError, ScraperConfig


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
