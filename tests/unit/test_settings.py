"""src/config.py::get_settings / get_paths / get_config — процесс читает только свой корень.

Корень берётся из `HUB_ROOT` до создания `Settings`, поэтому на временном корне
тест никогда не увидит `.env` разработчика в корне репозитория. Провайдеры
кэшированы на процесс; фикстура `_isolated_hub_root` чистит кэш вокруг теста.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import ConfigError, Settings, get_config, get_paths, get_settings

PROCESS_VARIABLES = (
    "APP_NAME", "ENVIRONMENT", "DEBUG", "API_PREFIX", "HOST", "PORT", "DOCS", "CORS_ORIGINS",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def _no_process_variables(monkeypatch):
    """Переменные окружения сильнее `.env`: чужие HOST/PORT/DOCS не должны мешать."""
    for name in PROCESS_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _write_env(root, text: str) -> None:
    (root / ".env").write_text(text, encoding="utf-8")


# ── откуда берётся .env ────────────────────────────────────────────────────


def test_get_settings_reads_the_env_file_under_hub_root(tmp_path):
    _write_env(tmp_path, "PORT=8123\n")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.port == 8123
    assert settings.hub_root == str(tmp_path)


def test_an_env_file_outside_hub_root_is_never_read(tmp_path, monkeypatch):
    """Как `.env` в корне репозитория: лежит рядом, но не в `HUB_ROOT`."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write_env(elsewhere, "PORT=1\n")
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HUB_ROOT", str(root))
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.port == 8000
    assert settings.hub_root == str(root)


def test_hub_root_inside_the_env_file_is_ignored(tmp_path):
    """Корень решает окружение; строка в `.env` не может увести процесс в другой каталог."""
    _write_env(tmp_path, f"HUB_ROOT={tmp_path / 'somewhere-else'}\n")
    get_settings.cache_clear()
    get_paths.cache_clear()

    assert get_settings().hub_root == str(tmp_path)
    assert get_paths().root == str(tmp_path)


def test_a_process_variable_wins_over_the_env_file(tmp_path, monkeypatch):
    _write_env(tmp_path, "PORT=8123\n")
    monkeypatch.setenv("PORT", "9001")
    get_settings.cache_clear()

    assert get_settings().port == 9001


def test_docs_and_cors_are_read_from_the_env_file(tmp_path):
    _write_env(tmp_path, 'DOCS=false\nCORS_ORIGINS=["http://localhost:5173"]\nENVIRONMENT=prod\n')
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.docs is False
    assert settings.cors_origins == ["http://localhost:5173"]
    assert settings.environment == "prod"


def test_get_settings_is_cached_until_cleared(tmp_path):
    first = get_settings()
    _write_env(tmp_path, "PORT=8123\n")

    assert get_settings() is first
    assert get_settings().port == 8000

    get_settings.cache_clear()
    assert get_settings().port == 8123


# ── проверки значений ──────────────────────────────────────────────────────


@pytest.mark.parametrize("port", [0, 70000], ids=["zero", "too-big"])
def test_a_port_outside_the_range_is_a_validation_error(port):
    with pytest.raises(ValidationError, match="port"):
        Settings(_env_file=None, port=port)


def test_a_non_numeric_port_in_the_env_file_is_a_validation_error(tmp_path):
    _write_env(tmp_path, "PORT=восемь\n")
    get_settings.cache_clear()

    with pytest.raises(ValidationError, match="port"):
        get_settings()


# ── пути и конфиг под корнем ───────────────────────────────────────────────


def test_paths_land_under_the_hub_root(tmp_path):
    paths = get_paths()

    assert paths.root == str(tmp_path)
    assert paths.db_path == str(tmp_path / "data" / "hub.db")
    assert paths.env_path == str(tmp_path / ".env")
    assert paths.config_path == str(tmp_path / "config.yaml")


def test_get_config_loads_the_yaml_under_the_hub_root(hub_paths, raw_config):
    cfg = get_config()

    assert cfg.scraper.user_agent == "test-agent"
    assert cfg.raw == raw_config


def test_get_config_without_a_config_yaml_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="config.yaml not found"):
        get_config()
