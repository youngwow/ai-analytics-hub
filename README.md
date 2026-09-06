# ai-analytics-hub

Кейс: «Интеллектуальный аналитический центр на базе ИИ» — автоматический сбор
отраслевых новостей, НПА и Telegram-дайджестов с последующей саммаризацией и
единой лентой (см. `context/task.md`).

## Быстрый старт

```bash
uv sync                                    # зависимости в .venv (Python ≥ 3.11)
cp .env.example .env                       # TAVILY_API — discover и search; TELEGRAM_API_* — MTProto; OLLAMA_API_KEY — обработка
uv run python -m src sources seed          # загрузить sources.json (53 источника, 44 включены)
uv run python -m src collect               # один проход по всем включённым источникам
uv run python -m src docs --limit 20       # что собрали
uv run python -m src process --limit 20    # обработать: саммари, тип, приоритет (нужен OLLAMA_API_KEY)
uv run python -m src items --priority high # лента: что важно прочитать первым
uv run python -m src collect --watch --interval 900   # опрашивать каждые 15 минут
uv run python -m src serve                 # HTTP-API на 127.0.0.1:8000, схема на /docs
docker compose up --build                  # то же в контейнере: data/ монтируется томом
```

`make install | seed | collect | watch | tg-login | tg-status | process | quality | serve | docker-up | test | lint | happy-pr | happy-gr` — те же команды.

Адрес, порт, `/docs` и CORS — параметры окружения (`HOST`, `PORT`, `DOCS`, `CORS_ORIGINS` в `.env`,
см. `.env.example`); корень данных переопределяется переменной `HUB_ROOT`.

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
| `docs [--source ID] [--limit N] [--unprocessed]` | Собранные документы; `--unprocessed` — то, у чего ещё нет карточки |
| `process [--limit N] [--source ID] [--since ISO] [--profile ID] [--force] [--dry-run] [--only-failed]` | Обработка документов без карточки: нормализация, кластеризация, один вызов модели на кластер. `--dry-run` показывает план без обращений к модели, `--only-failed` берёт только упавшее в прошлый прогон. Каждый прогон (кроме `--dry-run`) пишется в `processing_runs` |
| `item <id>` | Карточка целиком: саммари, сущности, источники, хронология НПА, история правок |
| `edit <id> [--summary …] [--priority …] [--type …] [--tags a,b] [--note …]` | Правка аналитика: поле помечается как правленое и не перезаписывается при переобработке. `--note` устарел и работает как `note <id>`: заметка уходит в `item_notes` |
| `reprocess <id> [--stages summary,priority] [--drop-human-edits]` | Переобработать карточку заново |
| `npa-event <id> --status S [--occurred-at ISO] [--note …]` | Ручное событие в хронологии НПА |
| `profile show \| list \| use <id> \| set <file.json>` | Профиль компании, по которому считается приоритет; `set` сохраняет новую версию |
| `quality [--from ISO] [--to ISO] [--gold]` | Метрики обработки; `--gold` считает recall по `high` и точность приоритета на размеченном наборе. По HTTP то же — `GET /api/v1/processing/quality` |
| `sources probe <url>` | Распознать ссылку и показать 3–5 последних материалов, ничего не сохраняя |
| `sources health <id>` | История опросов: когда, сколько нашлось, какая ошибка |
| `sources pause \| resume \| restore <id>` | Пауза, возврат в работу, восстановление удалённого источника |
| `sources refresh <id>` | Внеочередной опрос одного источника |
| `hide <id...> [--scope feed\|digest] [--reason …]` | Скрыть карточки: из ленты или из следующего дайджеста |
| `unhide <id...>` | Вернуть карточки в ленту |
| `note <id> --text …` | Заметка аналитика (во внешние API не уходит) |
| `revert <id> --field summary\|title\|priority\|tags` | Вернуть версию модели из истории правок |
| `revisions <id>` | История правок: кто, когда, было/стало |
| `add-item [--url …] --title … [--no-llm] [--force]` | Завести материал вручную; дубль распознаётся по адресу и по SimHash |
| `serve [--host] [--port] [--reload]` | Поднять HTTP-API (uvicorn, фабрика `src.main:create_app`), документация на `/docs` |
| `items [--q …] [--type] [--npa-status] [--priority]… [--tag]… [--source ID]… [--from] [--to] [--order] [--cursor] [--include-hidden]` | Лента: фильтры комбинируются, поиск идёт по карточке, тегам, сущностям и тексту оригинала |
| `digest [те же фильтры] [--format markdown\|json] [--include-notes] [--out FILE]` | Выгрузка среза для отправки руководителю |
| `status` | Когда собирали, сколько без карточки, какие источники просрочены |

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
4. **Разбор и проверка ответа.** Тип, размер саммари, диапазоны чисел и ссылки на текст
   проверяются по схеме; не прошедший ответ — один корректирующий повтор, затем экстрактивный
   baseline. Отдельного шага сверки саммари с оригиналом в коде нет (модуль удалён); вместо него
   работают два честных признака: `needs_review` при пограничной релевантности или низкой
   уверенности и `evidence_start/end` у сущностей, найденных в тексте дословно.

**Приоритет считается относительно профиля компании** (`profile show`) — отрасль, продукты,
регуляторы, ключевые темы и, что важнее, `negative_facets`: чем компания не является. Поэтому
законопроект о поддержке МСП получает `low`, хотя тематически он рядом. В пограничной зоне
(`relevance_score` 0.35–0.5) или при низкой уверенности приоритет **повышается** на ступень:
уронить критичный НПА в `low` дороже, чем показать лишнюю карточку.

**Если модель недоступна**, лента не пустеет: карточка собирается экстрактивным baseline'ом
(первые предложения, `priority = medium`), помечается `degraded`, и `process` возвращает код 2.
Повторный прогон переберёт такие документы заново.

**Правки аналитика не затираются.** `edit` помечает поле в `manual_overrides`, и `reprocess` его
не трогает; каждое изменение попадает в историю (`item_revisions`). Заметки аналитика живут
отдельно от саммари (`note <id> --text …`, таблица `item_notes`) и никогда не уходят в модель —
это проверяется отдельным тестом.

Ключ модели — `OLLAMA_API_KEY` в `.env` (ollama.com → Settings → Keys), модель и хост — в
`config.yaml`, секция `llm`. Тег модели должен совпадать с облачным буквально
(сейчас `glm-5.3-flash`). Переезд на другого провайдера меняет только реализацию `LLMProvider`.

Вложения (PDF и другие файлы) в 1.2 не читаются: обрабатывается текст самой публикации, а
материал, содержимое которого лежит во вложении, попадает в ленту ссылкой.

## Управление источниками и данными

Инструмент, а не замена аналитика: человек всегда главнее модели, и ничего не удаляется физически.

```bash
uv run python -m src sources probe https://www.cableman.ru/   # что это за ссылка?
uv run python -m src sources add https://www.cableman.ru/      # резолвер сам определит тип и ленту
uv run python -m src sources health 12                        # опрашивается ли источник и с какими ошибками
uv run python -m src hide 4 5 6 --scope digest                # готовим дайджест
uv run python -m src revert 7 --field summary                 # вернуть версию модели
uv run python -m src serve                                    # HTTP-API на :8000, /docs
```

**Ссылку не нужно разбирать руками.** `sources probe` гоняет её через тот же резолвер, что и сбор:
прямой RSS → `<link rel=alternate>` → типовые пути → `t.me/<канал>` → sitemap → HTML-список. В
ответе — тип, найденный адрес ленты, превью последних материалов и предупреждения
(`paywall_suspected`, `bot_protection`, `empty_feed`). Три написания одного канала (`t.me/x`,
`t.me/x/`, `t.me/s/x`) — один источник: повтор ловится по нормализованному адресу.

**У каждого источника своя периодичность** — 15 минут, час, 6 часов или сутки. При заведении её
подсказывает категория (регулятор — час, СМИ — шесть), меняется она по HTTP
(`PATCH /api/v1/sources/{id}` с `poll_interval`); отдельной команды в CLI нет. Отдельного
планировщика тоже нет: `collect --watch` или наблюдатель API тикают часто и опрашивают только тех,
чья очередь пришла. Смена интервала действует сразу, а не с конца текущего периода.

**Падающий источник опрашивается реже.** После неудачи интервал растягивается вдвое за каждую
попытку подряд (до шестнадцати), первый успех возвращает обычный ритм. Источник, который молчит
неделю и полсотни попыток, сам уходит в паузу с причиной в примечании — возвращается обычным
`sources resume`.

**Каждый опрос попадает в историю** (`sources health`): когда, сколько нашлось, сколько новых,
машинный код ошибки. Это единственный способ отличить «источник молчит» от «источник сломался
неделю назад» — незамеченный сбой мониторинга дороже шума в ленте.

**Правки не затираются.** Поле, которое правил человек, попадает в `manual_overrides`, и
переобработка его не трогает; версия модели при этом сохраняется в истории, поэтому
`revert --field summary` возвращает её одним действием. Заметки аналитика живут отдельно от
саммари и никогда не уходят в модель.

**Удаления нет.** Карточка скрывается из ленты или из следующего дайджеста и остаётся в базе и в
поиске; источник помечается удалённым, а собранные из него материалы остаются — НПА живёт дольше,
чем ссылка, по которой он пришёл.

HTTP-API повторяет те же операции по адресам `/api/v1/sources` и `/api/v1/items` (плюс
`POST /api/v1/sources/{id}/refresh` — внеочередной опрос для демо; `PATCH /sources/{id}` принимает
и `url` / `type` / `fetch_url` — источник переезжает на другой адрес без второй записи); ошибки —
`application/problem+json` с машинным `code`, у каждого ответа есть схема в `/openapi.json`.
Логики в обработчиках нет: они зовут те же методы сервисов, что и CLI, поэтому дашборд 1.3
добавит экраны, а не вторую реализацию. `GET /api/v1/health` и `/health/ready` — для
оркестратора: готовность падает в 503, когда база не отвечает.

Из дашборда доступны и операции, которые раньше были только в CLI:

- **очередь ИИ** — `POST /api/v1/processing/runs` запускает прогон обработки в фоне (202 и запись
  прогона), `GET /processing` и `GET /processing/runs[/{id}]` показывают, идёт ли обработка и чем
  кончились прошлые; одновременно идёт один прогон (`409 processing_busy`); история — в таблице
  `processing_runs` (миграция v5), её же ведёт `python -m src process`;
- **автоматический мониторинг** — `POST /api/v1/collection/start` / `stop` включают фоновый цикл
  сбора внутри процесса API (тот же `collect --watch`: опрашиваются только те, чья очередь пришла),
  `GET /collection` — состояние и последний цикл, `POST /collection/runs` — разовый проход в фоне;
- **события НПА и сроки** — `POST /api/v1/items/{id}/events` (`status`, `occurred_at`, `note`):
  статус из словаря двигает карточку вперёд, любое другое событие (слушания, срок) ложится в
  хронологию;
- **архив** — `POST /api/v1/items/{id}/archive` / `unarchive`: карточка уходит из ленты и
  дайджеста, но остаётся в поиске; параметр ленты `archived=exclude|include|only`;
- **очередь документов постранично** — `GET /api/v1/documents` отдаёт `next_cursor`, как и лента;
  плюс `order=published|fetched` и поиск по подстроке, где `%` и `_` — символы, а не шаблон;
- **массовые правки** — `POST /api/v1/items/tags/bulk` (добавить и снять теги у списка карточек) и
  `POST /api/v1/items/archive/bulk`: тегинг и архивация пачкой вместо двадцати одиночных PATCH;
- **профиль компании** — `GET /api/v1/profiles`, `/profiles/active`, `POST /profiles` (новая версия,
  а не правка на месте) и `POST /profiles/{id}/default`: смена того, что считается «высоким
  приоритетом», перестала быть операцией из терминала;
- **выгрузка среза** — `GET /api/v1/export/feed.xml` (RSS: подписать соседний отдел на живую ленту)
  и `GET /api/v1/export/items.csv` (для Excel). Фильтры те же, что у ленты; отдаётся только видимое,
  без архива и без заметок аналитика;
- **сводка качества** — `GET /api/v1/processing/quality`: карточки, очередь и вызовы модели с
  разбивкой по этапам и суткам (то же, что печатает `python -m src quality`);
- **повтор сбойных** — документ помнит свою ошибку обработки (`attempts`, `last_error`), поэтому
  `process --only-failed` и `{"only_failed": true}` берут только упавшее в прошлый прогон. Очередь
  при этом уважает категорию источника: НПА от регулятора не ждёт за лентой СМИ.

## Лента, поиск и дайджест

```bash
uv run python -m src items --priority high --from 2026-09-01        # срез повестки
uv run python -m src items --q КИИ                                  # поиск шире карточки
uv run python -m src digest --priority high --format markdown       # выжимка руководителю
uv run python -m src status                                         # почему в ленте столько
curl -s 'localhost:8000/api/v1/items/facets?from=2026-09-01' | jq   # счётчики среза
```

**Фильтры комбинируются**: несколько приоритетов и источников — по «или», несколько тегов — по
«и». `total` в ответе считается по срезу, а не по всей базе, поэтому на него можно опираться.
Листание — курсором, а не смещением: при притоке около 600 документов в сутки смещение давало бы
и дубли, и пропуски.

**Поиск шире карточки.** Индекс собирается из заголовка, саммари, тегов, значений сущностей и
полного текста оригинала, поэтому слово, не попавшее в саммари из 3–5 предложений, всё равно
находится. Морфологии у SQLite нет, поэтому запрос префиксный: «законопроект» находит
«законопроекта», но и «акт» найдёт «активы». Каждая найденная карточка приходит с фрагментом
(`snippet`) — иначе непонятно, почему она в выдаче.

**Даты — местные.** Голая дата (`--from 2026-09-05`) означает сутки по `api.timezone`
(по умолчанию `Europe/Moscow`), а не по UTC: иначе «за сегодня» теряет первые три часа
московского утра. ISO-время со смещением берётся как прислано.

**Скрытие работает по-разному.** `hide --scope feed` убирает карточку из ленты; `--scope digest`
оставляет её в ленте и убирает только из выгрузки — лента одна на всех, и подготовка адресного
дайджеста не должна чистить её остальным.

**Пустая лента объясняет себя.** `status` показывает разрыв между собранным и обработанным: если
документов на порядок больше, чем карточек, дело не в источниках, а в очереди обработки — `docs
--unprocessed` покажет, что именно ждёт очереди, а `GET /api/v1/processing` — идёт ли прогон.

Те же операции доступны по HTTP: `/api/v1/items`, `/items/facets`, `/filters`, `/documents`,
`/digest`, `/status`. Логики в обработчиках нет — они зовут те же методы `FeedService`, что и CLI.

## Источники

`sources.json` собран **только** из `context/sources_for_company.md` и проверен вручную: адреса
лент закреплены через `kind`/`fetch_url`, чтобы `sources seed` не ходил в сеть. Всего 53 источника,
44 включены; ещё 8 адресов лежат в `excluded` с причиной.

- **СМИ (17)** — CNews и CNews Телеком, TAdviser, Коммерсантъ, Lenta.ru, Интерфакс, Телеспутник,
  Кабельщик (sitemap), D-Russia, Habr, КонсультантПлюс, Ведомости («Технологии»); выключены до
  проверки из РФ или как низкоприоритетные: РБК, ТАСС, RusCable, Forbes.
- **Регуляторы (14)** — официальное опубликование НПА `publication.pravo.gov.ru` (три ленты API:
  Правительство, ФОИВ, Президент), government.ru, Роскомнадзор, Госдума, ФНС, ФСБ (HTML-diff),
  РФРИТ (sitemap), Гарант, «Банк России — новости»; выключены общая лента опубликования и
  пресс-релизы ЦБ.
- **Telegram (22)** — каналы через MTProto или зеркала `t.me/s/`: Минцифры, Правительство, Госдума,
  РФРИТ, АРПП, АРПЭ, RSpectr, ЦИПР, CIO, ComNews, TAdviser, CNews, Кабельщик, Коммерсантъ,
  Ведомости, Телеспутник, «Цифровая экономика», ТАСС, ЦИТ, ФСИ, «Гранты для ИТ»; Торгпред выключен.
- **Поиск (2, выключены; посчитаны в категориях выше)** — запросы Tavily «упоминания GS Labs /
  Триколор» (news, 7 дней, домены отраслевых СМИ) и «проекты НПА — ПО, ПД, КИИ» (general, 14 дней,
  regulation.gov.ru, СОЗД, pravo.gov.ru, government.ru, Минцифры). Каждый `collect` по ним стоит
  кредиты Tavily — включайте осознанно (`sources enable <id>`).
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

process ──► S0 нормализация ──► S1 SimHash + косинус на numpy ──► кластер
                                       │
            один вызов модели на кластер (тип, сущности, саммари, приоритет, теги)
                                       │
            разбор и проверка ответа ──► items + entities + item_sources (транзакция на кластер)
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
- `src/services/` — слой сервисов, один на CLI и HTTP: `ProcessingService` (прогон и его запись в
  `processing_runs`, ручной материал, переобработка — всё, что зовёт модель; медленные вызовы — в
  воркерах, записи — на главном потоке), `ItemService` (правка, видимость, архив, события, заметки,
  история, массовые операции), `ProfileService` (профиль компании: версии, профиль по умолчанию), `SourceService` (probe, расписание, здоровье, `refresh`, переезд на другой адрес),
  `FeedService` (лента, фасеты, документы с курсором, дайджест, `status`), `CollectionService` +
  `CollectionWatcher` (фоновый цикл сбора в процессе API), `HealthService`.
- `src/processing/llm.py` — провайдер модели, единственный модуль с `import ollama`; наружу торчат
  протоколы `LLMProvider` / `EmbeddingProvider`, поэтому тесты не знают об SDK.
- `src/processing/pipeline.py` — S2–S5 над одним кластером: один structured-output вызов, проверка
  ответа, fail-safe по приоритету (он же поднимает `needs_review`), экстрактивный baseline при
  отказе модели.
- `src/processing/dedup.py`, `normalize.py` — SimHash и косинус на numpy (матрица кандидатов
  собирается раз на прогон: один matvec на документ вместо 400 тысяч сравнений), нормализация с
  сохранением offsets.
- `src/repositories/` — `Database` (`data/hub.db`, соединение на запрос, миграции по
  `PRAGMA user_version`, сейчас v8) и SQLite-репозитории по агрегатам: `sources`, `documents`,
  `items`, `processing`, `feed` (читающие запросы ленты). Миграция применяется целиком или никак:
  DDL, донаполнение и сама версия лежат в одной транзакции. Контракты для сервисов —
  `repository_interface.py` (`SourceRepository`, `DocumentRepository`, `ItemRepository`,
  `FeedRepository`).
- `src/models/` — доменные dataclass'ы (`domain.py`), фильтр ленты (`queries.py`) и pydantic-схемы
  HTTP-границы (`requests.py`, `responses.py` с `from_domain()`); дальше границы pydantic не идёт.
- `src/api/routes/` — тонкие маршруты (`health`, `sources`, `items`, `feed`, `processing`,
  `collection`, `profiles`, `export`); `src/dependencies.py` —
  единственное место связывания (`Annotated[..., Depends()]`); `src/exceptions.py` — иерархия
  `AppError` со статусом и кодом на классе; `src/main.py` — фабрика `create_app()`, lifespan, CORS,
  единый обработчик ошибок в `problem+json`.
- `src/config.py` — `Config` из `config.yaml` (домен) и `Settings` из окружения/`.env` (процесс:
  адрес, порт, CORS, уровень логов, `HUB_ROOT`). `src/Dockerfile` + `docker-compose.yaml` —
  контейнер с healthcheck на `/api/v1/health/ready`.
- `config.yaml` — окно первого сбора, таймауты, лимиты, параметры Tavily (`days`, `country`, `language`)
  и Telegram (`mtproto`, `max_posts`, `concurrency`), модель и пороги обработки (`llm`, `processing`).

## Разработка

```bash
uv run pytest -q             # тесты офлайн, HTTP через httpx.MockTransport + tests/fixtures/
uv run ruff check src tests  # make lint / make test — те же команды
uv run python -m src serve --reload   # API с автоперезапуском; или make docker-up
```

Тесты собирают приложение фабрикой `create_app()` на временном `HUB_ROOT`, поэтому `.env`
разработчика в тестах не читается; модель подменяется через `app.dependency_overrides`.

Проектная документация — в `docs/`.
