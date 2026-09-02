# ai-analytics-hub

Кейс: «Интеллектуальный аналитический центр на базе ИИ» — автоматический сбор
отраслевых новостей, НПА и Telegram-дайджестов с последующей саммаризацией и
единой лентой (см. `context/task.md`).

Сейчас реализован **этап 1.1 — сбор данных**: один резолвер типа источника
плюс адаптеры (RSS/Atom, Telegram через `t.me/s/`, sitemap, обход HTML-страницы),
полный текст через trafilatura, хранение в SQLite. Этапы 1.2–1.4
(саммаризация, категоризация, дашборд, управление источниками из UI) — впереди.

## Быстрый старт

```bash
uv sync                         # зависимости в .venv (Python ≥ 3.11)
cp .env.example .env            # TAVILY_API нужен только для `discover`
uv run python -m src sources seed          # загрузить sources.json (14 источников кейса + регуляторы)
uv run python -m src collect               # один проход по всем включённым источникам
uv run python -m src docs --limit 20       # что собрали
uv run python -m src collect --watch --interval 900   # опрашивать каждые 15 минут
```

`make install | seed | collect | watch | test | lint` — те же команды.

## Команды

| Команда | Что делает |
|---|---|
| `collect [--source ID] [--backfill] [--force] [--watch --interval S]` | Опрос источников: условный GET для RSS, курсор по постам для Telegram, `lastmod` для sitemap. `--backfill` листает историю, `--force` сбрасывает ETag/курсоры |
| `sources add <url> [--name] [--category] [--kind --fetch-url]` | Вставили ссылку — резолвер сам решает, чем её тянуть; `--kind/--fetch-url` закрепляют адрес вручную |
| `sources list / enable / disable / remove / resolve <id>` | Состояние источников (документы, последний успех, ошибка) и управление ими |
| `sources seed [file]` | Загрузка стартового списка из `sources.json` |
| `resolve <url>` | Сухой прогон резолвера: какой адаптер и какой адрес |
| `discover "<запрос>" [--domains gov.ru,...] [--add]` | Поиск источников через Tavily (дополнение, не замена: западные поисковики плохо индексируют gov.ru) |
| `import-url <url>` | Разовый импорт страницы в источник «Ручной импорт» |
| `docs [--source ID] [--limit N]` | Просмотр собранных документов |

## Как устроено

```
URL источника → resolver → kind (rss | telegram | sitemap | html | manual)
                                       │
collect ──► ThreadPool ──► adapter.fetch() ──► RawDocument[] ──► dedup ──► fulltext ──► SQLite
             (HTTP+parse)                                     (main thread, транзакция на источник)
```

- `src/sources/resolver.py` — цепочка из `scraper.md` §0: t.me → RSS по URL → `<link rel=alternate>` и типовые пути → sitemap с `lastmod` → HTML-diff.
- `src/sources/scraper_*.py` — адаптеры с общим интерфейсом (`base.py`); все возвращают `RawDocument`
  (`source_id, external_id, url, title, summary, text, author, attachments, published_at, fetched_at, content_hash`).
- `src/sources/fulltext.py` — trafilatura для материалов, где в ленте только анонс; не больше двух
  одновременных запросов к одному хосту (gov.ru банит бурсты).
- `src/storage/db.py` — `data/hub.db`: `sources`, `documents`, `fetch_state`, `seen_urls`, `collect_runs`.
- `config.yaml` — окно первого сбора, таймауты, лимиты, параметры Tavily.

## Разработка

```bash
uv run pytest -q             # тесты офлайн, HTTP через httpx.MockTransport + tests/fixtures/
uv run ruff check src tests
```

Проектная документация — в `docs/`.
