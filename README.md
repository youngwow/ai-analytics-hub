# ai-analytics-hub

Кейс: «Интеллектуальный аналитический центр на базе ИИ» — автоматический сбор
отраслевых новостей, НПА и Telegram-дайджестов с последующей саммаризацией и
единой лентой (см. `context/task.md`).

Реализован **этап 1.1 — сбор данных**: один резолвер типа источника плюс адаптеры
(RSS/Atom, Telegram через MTProto или `t.me/s/`, sitemap, обход HTML-страницы, поисковые
запросы Tavily), полный текст через trafilatura, хранение в SQLite. Пул источников —
из `context/sources_for_company.md` (GS Labs).

Реализован **этап 1.2 — интеллектуальная обработка**: собранные документы схлопываются в
кластеры и превращаются в карточки ленты — саммари в 3–5 предложений, сущности «кто / что /
когда / последствия», тип (`НПА` | `Новость`), приоритет относительно профиля компании и теги.
Модель — GLM-5.3 в Ollama Cloud за интерфейсом `LLMProvider`. Этапы 1.3–1.4 (дашборд,
управление источниками из UI) — впереди.

## Быстрый старт

```bash
uv sync                                    # зависимости в .venv (Python ≥ 3.11)
cp .env.example .env                       # TAVILY_API — discover и search; TELEGRAM_API_* — MTProto; OLLAMA_API_KEY — обработка
uv run python -m src sources seed          # загрузить sources.json (41 источник, 32 включены)
uv run python -m src collect               # один проход по всем включённым источникам
uv run python -m src docs --limit 20       # что собрали
uv run python -m src process --limit 20    # обработать: саммари, тип, приоритет (нужен OLLAMA_API_KEY)
uv run python -m src items --priority high # лента: что важно прочитать первым
uv run python -m src collect --watch --interval 900   # опрашивать каждые 15 минут
```

`make install | seed | collect | watch | tg-login | tg-status | process | quality | test | lint | happy-pr | happy-gr` — те же команды.

## Команды

| Команда | Что делает |
|---|---|
| `collect [--source ID] [--backfill] [--force] [--watch --interval S]` | Опрос источников: условный GET для RSS, курсор по постам для Telegram, `lastmod` для sitemap, окно `days` для поисковых запросов. `--backfill` листает историю, `--force` сбрасывает ETag/курсоры |
| `search "<запрос>" [--domains a.ru,b.ru \| @media \| @regulator \| @all] [--days N] [--general] [--no-summary] [--category media\|regulator] [--save] [--name …]` | Новости по запросу через Tavily за последние N дней: каждое попадание и «Сводка» (ответ Tavily) сохраняются в `documents`. Запрос становится источником вида `search`; с `--save` он включён и `collect` опрашивает его дальше |
| `telegram login [--phone …] / status / logout --yes` | Вход в Telegram через MTProto: разовая интерактивная авторизация (телефон, код, 2FA), проверка сессии, удаление сессии. Без неё Telegram собирается через веб-превью |
| `discover "<запрос>" [--domains …] [--add]` | Поиск *источников* через Tavily: сайты из результатов прогоняются через резолвер и добавляются (`--add`) |
| `sources add <url> [--name] [--category] [--kind --fetch-url]` | Вставили ссылку — резолвер сам решает, чем её тянуть; `--kind/--fetch-url` закрепляют адрес вручную |
| `sources list / enable / disable / remove / resolve <id>` | Состояние источников (документы, последний успех, ошибка) и управление ими |
| `sources seed [file]` | Загрузка стартового списка из `sources.json` (`"enabled": false` — добавить выключенным) |
| `resolve <url>` | Сухой прогон резолвера: какой адаптер и какой адрес |
| `import-url <url>` | Разовый импорт страницы в источник «Ручной импорт» |
| `docs [--source ID] [--limit N]` | Просмотр собранных документов |
| `process [--limit N] [--source ID] [--since ISO] [--profile ID] [--force] [--dry-run]` | Обработка документов без карточки: нормализация, кластеризация, один вызов модели на кластер, проверка саммари на опору в оригинале. `--dry-run` показывает план без обращений к модели |
| `items [--type npa\|news] [--priority …] [--tag T] [--q ТЕКСТ] [--limit N]` | Лента карточек с фильтрами; `--q` ищет по заголовку и саммари через FTS5 |
| `item <id>` | Карточка целиком: саммари, сущности, источники, хронология НПА, история правок |
| `edit <id> [--summary …] [--priority …] [--type …] [--tags a,b] [--note …]` | Правка аналитика: поле помечается как правленое и не перезаписывается при переобработке |
| `reprocess <id> [--stages summary,priority] [--drop-human-edits]` | Переобработать карточку заново |
| `npa-event <id> --status S [--occurred-at ISO] [--note …]` | Ручное событие в хронологии НПА |
| `profile show \| list \| use <id> \| set <file.json>` | Профиль компании, по которому считается приоритет; `set` сохраняет новую версию |
| `quality [--from ISO] [--to ISO] [--gold]` | Метрики обработки; `--gold` считает recall по `high` и точность приоритета на размеченном наборе |

## Обработка: саммари, категория, приоритет

`collect` наполняет `documents`, `process` превращает их в карточки ленты (`items`).

```bash
uv run python -m src process --limit 20     # обработать 20 свежих документов
uv run python -m src items --priority high  # что читать первым
uv run python -m src item 42                # карточка целиком
```

Что происходит с каждым документом:

1. **Нормализация.** Чистка HTML и служебных строк; результат хранится в `documents.norm_text`,
   потому что `evidence_offsets` считаются именно по нему.
2. **Дедупликация.** URL → SimHash по 3-граммам → косинус по эмбеддингам, но только среди
   кандидатов за последние 7 дней. 15 перепечаток одной новости дают одну карточку и **один**
   вызов модели, а не пятнадцать.
3. **Один structured-output вызов** на кластер: тип, сущности, саммари, приоритет и теги сразу.
   Схема уходит провайдеру параметром `format`, поэтому синтаксически битый JSON исключён.
4. **Проверка на опору в оригинале.** Каждое предложение саммари обязано ссылаться на фрагмент
   текста, а числа, даты и месяцы из предложения — встречаться в этом фрагменте. Не прошедшее
   предложение удаляется; если снято больше половины, карточка помечается «нужна проверка».

**Приоритет считается относительно профиля компании** (`profile show`) — отрасль, продукты,
регуляторы, ключевые темы и, что важнее, `negative_facets`: чем компания не является. Поэтому
законопроект о поддержке МСП получает `low`, хотя тематически он рядом. В пограничной зоне
(`relevance_score` 0.35–0.5) или при низкой уверенности приоритет **повышается** на ступень:
уронить критичный НПА в `low` дороже, чем показать лишнюю карточку.

**Если модель недоступна**, лента не пустеет: карточка собирается экстрактивным baseline'ом
(первые предложения, `priority = medium`), помечается `degraded`, и `process` возвращает код 2.
Повторный прогон переберёт такие документы заново.

**Правки аналитика не затираются.** `edit` помечает поле в `edited_fields`, и `reprocess` его
не трогает; каждое изменение попадает в историю (`item_revisions`). Заметка `analyst_note`
никогда не уходит в модель — это проверяется отдельным тестом.

Ключ модели — `OLLAMA_API_KEY` в `.env` (ollama.com → Settings → Keys), модель и хост — в
`config.yaml`, секция `llm`. Тег модели должен совпадать с облачным буквально
(`glm-5.3:cloud`). Переезд на другого провайдера меняет только реализацию `LLMProvider`.

Вложения (PDF и другие файлы) в 1.2 не читаются: обрабатывается текст самой публикации, а
материал, содержимое которого лежит во вложении, попадает в ленту ссылкой.

## Источники

`sources.json` собран **только** из `context/sources_for_company.md` и проверен вручную
03.09.2026 (адреса лент закреплены через `kind`/`fetch_url`, чтобы `sources seed` не ходил
в сеть):

- **СМИ (14)** — CNews, TAdviser, Коммерсантъ, ТАСС, Lenta.ru, Телеспутник, Кабельщик (sitemap),
  Habr, Ведомости (рубрика «Технологии»); выключены до проверки из РФ или как низкоприоритетные:
  РБК, D-Russia, КонсультантПлюс, RusCable, Forbes.
- **Регуляторы (9)** — официальное опубликование НПА `publication.pravo.gov.ru` (три ленты API:
  Правительство, ФОИВ, Президент), government.ru, Роскомнадзор, ФНС, ФСБ (HTML-diff), РФРИТ
  (sitemap); ЦБ выключен как низкоприоритетный.
- **Telegram (16)** — каналы через MTProto или зеркала `t.me/s/`: Минцифры, Правительство, РФРИТ, АРПП, АРПЭ, RSpectr,
  ЦИПР, CIO, ComNews, TAdviser, CNews, Кабельщик, «Цифровая экономика», ТАСС, ФСИ; Торгпред выключен.
- **Поиск (2, выключены)** — запросы Tavily «упоминания GS Labs / Триколор» (news, 7 дней, домены
  отраслевых СМИ) и «проекты НПА — ПО, ПД, КИИ» (general, 14 дней, regulation.gov.ru, СОЗД,
  pravo.gov.ru, government.ru, Минцифры). Каждый `collect` по ним стоит кредиты Tavily —
  включайте осознанно (`sources enable <id>`).
- **`excluded`** — ComNews, Broadcasting.ru, RSpectr (сайты не отвечают из-за рубежа; их Telegram-каналы
  включены), regulation.gov.ru и СОЗД (нужны адаптеры по API, пока закрыты поисковым источником),
  digital.gov.ru и ФСТЭК (таймаут / сертификат российского УЦ), реестр ПО (только поиск с параметрами).

Государственные домены и часть СМИ ограничивают доступ с зарубежных IP: с российского адреса
выключенные и исключённые источники стоит перепроверить (`sources resolve <id>`, `sources add <url>`).

## Telegram: MTProto или веб-превью

Каналы читаются двумя способами, тип источника у обоих один — `telegram`, номера постов
совпадают, поэтому переключение транспорта не приводит ни к повторному импорту, ни к дублям.

| | `t.me/s/<канал>` | MTProto (Telethon) |
|---|---|---|
| Что нужно | ничего | `api_id`/`api_hash` с [my.telegram.org](https://my.telegram.org) и один вход |
| Каналы без веб-превью | не читаются | читаются |
| Догоняние | до 5 страниц `?after=` за проход | `telegram.max_posts` за проход, `backfill_posts` при `--backfill` |
| Вложения | ссылки `t.me` | имена файлов (`file:<имя>` в `attachments`); сами файлы не скачиваются |

```bash
uv run python -m src telegram login     # телефон → код из Telegram → пароль 2FA, если включён
uv run python -m src telegram status    # ключи, файл сессии, аккаунт, режим
uv run python -m src collect            # Telegram-источники пойдут через MTProto
```

`telegram.mtproto` в `config.yaml`: `auto` (MTProto при наличии сессии, иначе превью), `off`
(только превью), `only` (без сессии источник падает с ошибкой). В режиме `auto` ошибка по одному
каналу откатывает на превью, а проблема с самой сессией отключает MTProto до конца прогона.

Файл сессии `data/telegram.session` — это доступ к аккаунту: он в `.gitignore`, его нельзя
коммитить и пересылать. Чтение пассивное: каналы не подписываются, посты не помечаются
прочитанными, одновременных запросов не больше `telegram.concurrency` (по умолчанию 2).

## Поиск через Tavily

`search` дополняет ленты, а не заменяет их: западные поисковые API плохо индексируют gov.ru и
нишевые российские СМИ, поэтому качество ответа сильно зависит от `--domains`. Что происходит:

1. Запрос сохраняется как источник `tavily://search?q=…&domains=…&days=N&topic=news|general&summary=1`
   (одинаковый запрос → тот же источник).
2. Первый запуск и `--force` берут последние `N` дней; следующие `collect` — только то, что появилось
   после предыдущего опроса (`start_date`).
3. Каждое попадание — обычный документ: текст со страницы, которую вернул Tavily (или дотянутый
   trafilatura), дата публикации (для `news`). Ответ Tavily по запросу — документ «Сводка: <запрос>»,
   один в день; `tavily.language: ru` в `config.yaml` просит ответ по-русски (Tavily соблюдает это не всегда).
4. `include_answer=advanced` и `search_depth=advanced` стоят дороже базового кредита.

## Сценарии: PR и GR (happy path)

Оба сценария — исполняемые скрипты, повторный запуск безопасен. Шаги с Tavily пропускаются,
если `TAVILY_API` не задан.

### PR-менеджер — репутация (`scripts/happy_path_pr.sh`, `make happy-pr`)

```bash
uv run python -m src sources seed            # 1. пул источников: СМИ, регуляторы, Telegram-зеркала
uv run python -m src collect                 # 2. первый сбор (окно 72 ч, до 50 материалов с источника)
uv run python -m src docs --limit 15         # 3. что пришло
uv run python -m src search "GS Labs OR Триколор OR StingrayTV" --domains @media --days 7 --save \
    --name "Поиск: GS Labs / Триколор за неделю"   # 4. упоминания за неделю по отраслевым СМИ; запрос сохранён,
                                                    #    попадания и «Сводка» — в documents
uv run python -m src search "система условного доступа CAS DRM спутниковое телевидение" \
    --domains @media --days 30               # 5. разовый запрос по продуктовой теме за месяц
uv run python -m src sources list            # 6. состояние источников
uv run python -m src collect --watch --interval 900   # дальше: опрос каждые 15 минут, включая сохранённый поиск
```

### GR-специалист — НПА и регуляторы (`scripts/happy_path_gr.sh`, `make happy-gr`)

```bash
uv run python -m src sources seed            # 1. пул источников
uv run python -m src collect                 # 2. pravo.gov.ru (Правительство, ФОИВ, Президент), government.ru,
                                             #    Роскомнадзор, ФНС, РФРИТ, каналы Минцифры / АРПП / АРПЭ
uv run python -m src docs --source <id «акты Правительства»> --limit 10   # 3. лента регулятора
uv run python -m src search "законопроект персональные данные КИИ реестр отечественного ПО" \
    --domains regulation.gov.ru,sozd.duma.gov.ru,publication.pravo.gov.ru,government.ru,digital.gov.ru \
    --general --days 14 --category regulator --save --name "Поиск: НПА по ПО, ПД, КИИ"
                                             # 4. проекты НПА там, где лент нет; запрос сохранён для collect
uv run python -m src search "аккредитация ИТ-компаний налоговые льготы" \
    --domains @regulator,digital.gov.ru --general --days 30 --category regulator   # 5. разовый запрос: пресет + домен
uv run python -m src import-url http://publication.pravo.gov.ru/document/<номер>   # 6. ручное добавление акта
uv run python -m src sources disable <id Lenta.ru>   # 7. адресная лента: убираем широкий новостной фон
uv run python -m src sources list            # 8. состояние источников
uv run python -m src collect --watch --interval 3600  # дальше: сайты регуляторов — раз в час
```

## Как устроено

```
URL источника → resolver → kind (rss | telegram | sitemap | html | search | manual)
                                       │
collect ──► ThreadPool ──► adapter.fetch() ──► RawDocument[] ──► dedup ──► fulltext ──► SQLite
             (HTTP+parse)                                     (main thread, транзакция на источник)
search  ──► collect_one(источник-запрос) ──► SearchAdapter → Tavily ──► документы + «Сводка»

process ──► S0 нормализация ──► S1 SimHash + эмбеддинги ──► кластер
                                       │
            один вызов модели на кластер (тип, сущности, саммари, приоритет, теги)
                                       │
            S6 проверка опоры на оригинал ──► items + entities + item_sources (транзакция на кластер)
```

- `src/sources/resolver.py` — цепочка из `scraper.md` §0: t.me → `tavily://` → RSS по URL →
  `<link rel=alternate>` и типовые пути → sitemap с `lastmod` → HTML-diff.
- `src/sources/telegram_mtproto.py` — Telethon за синхронным интерфейсом: один фоновый event loop и
  один клиент на прогон, ошибки сводятся к «канал недоступен» и «сессия непригодна».
- `src/sources/scraper_*.py` — адаптеры с общим интерфейсом (`base.py`); все возвращают `RawDocument`
  (`source_id, external_id, url, title, summary, text, author, attachments, published_at, fetched_at, content_hash`).
  `scraper_search.py` — запрос Tavily как источник; `scraper_llm.py` — HTTP-обёртка Tavily.
- `src/sources/fulltext.py` — trafilatura для материалов, где в ленте только анонс; не больше двух
  одновременных запросов к одному хосту (gov.ru банит бурсты).
- `src/processing/service.py` — `ProcessingService`: единственная точка входа этапа 1.2, на неё сядет
  и будущий REST. Медленные вызовы модели — в воркерах, все записи — на главном потоке.
- `src/processing/llm.py` — провайдер модели, единственный модуль с `import ollama`; наружу торчат
  протоколы `LLMProvider` / `EmbeddingProvider`, поэтому тесты не знают об SDK.
- `src/processing/pipeline.py` — S2–S6 над одним кластером: один structured-output вызов, проверка
  ответа, fail-safe по приоритету, экстрактивный baseline при отказе модели.
- `src/processing/dedup.py`, `normalize.py`, `grounding.py` — SimHash и косинус без numpy,
  нормализация с сохранением offsets, проверка саммари на опору в оригинале.
- `src/storage/db.py` — `data/hub.db`: `sources`, `documents`, `fetch_state`, `seen_urls`,
  `collect_runs` (v1) плюс `clusters`, `items`, `entities`, `item_sources`, `npa_events`,
  `item_revisions`, `company_profiles`, `prompt_versions`, `llm_calls` и `items_fts` (v2).
- `config.yaml` — окно первого сбора, таймауты, лимиты, параметры Tavily (`days`, `country`, `language`)
  и Telegram (`mtproto`, `max_posts`, `concurrency`), модель и пороги обработки (`llm`, `processing`).

## Разработка

```bash
uv run pytest -q             # тесты офлайн, HTTP через httpx.MockTransport + tests/fixtures/
uv run ruff check src tests
```

Проектная документация — в `docs/`.
