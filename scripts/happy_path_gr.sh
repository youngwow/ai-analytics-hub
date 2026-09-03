#!/usr/bin/env bash
# Happy path для GR-специалиста: НПА и инициативы регуляторов — опубликованные акты, проекты, льготы.
# Запуск из корня репозитория: bash scripts/happy_path_gr.sh
# Повторный запуск безопасен: источники не дублируются, уже сохранённые материалы пропускаются.
set -euo pipefail
cd "$(dirname "$0")/.."

hub() { uv run python -m src "$@"; }
have_key() { [ -n "${TAVILY_API:-}" ] || grep -Eq '^TAVILY_API=.+' .env 2>/dev/null; }
# id источника по фрагменту названия из `sources list`
src_id() { hub sources list | awk -v pat="$1" '$0 ~ pat {print $1; exit}'; }

echo "▶ 1. Пул источников из context/sources_for_company.md"
hub sources seed

echo "▶ 2. Сбор: официальное опубликование (pravo.gov.ru — Правительство, ФОИВ, Президент), government.ru,"
echo "     Роскомнадзор, ФНС, РФРИТ, каналы Минцифры / АРПП / АРПЭ"
hub collect

echo "▶ 3. Лента регулятора: свежие акты Правительства"
hub docs --limit 10 --source "$(src_id 'акты Правительства')"

if have_key; then
  echo "▶ 4. Проекты НПА по профилю компании за две недели там, где лент нет (regulation.gov.ru, СОЗД, pravo.gov.ru,"
  echo "     Минцифры): сохраняем как источник, collect будет опрашивать его дальше"
  hub search "законопроект персональные данные КИИ реестр отечественного ПО" \
    --domains regulation.gov.ru,sozd.duma.gov.ru,publication.pravo.gov.ru,government.ru,digital.gov.ru \
    --general --days 14 --category regulator --save --name "Поиск: НПА по ПО, ПД, КИИ"

  echo "▶ 5. Разовый запрос: льготы и аккредитация ИТ-компаний за месяц по сайтам включённых регуляторов и Минцифры"
  out=$(hub search "аккредитация ИТ-компаний налоговые льготы" \
    --domains @regulator,digital.gov.ru --general --days 30 --category regulator | tee /dev/stderr)

  echo "▶ 6. Ручное добавление документа, которого нет в источниках: акт с pravo.gov.ru из результатов шага 5"
  url=$(printf '%s\n' "$out" | grep -o 'http://publication.pravo.gov.ru/document/[0-9]*' | head -1 || true)
  hub import-url "${url:-http://publication.pravo.gov.ru/document/0001202609020026}"
else
  echo "▶ 4–6. Поиск через Tavily пропущен: задайте TAVILY_API в .env (cp .env.example .env)"
  echo "▶ 6. Ручное добавление документа, которого нет в источниках"
  hub import-url "http://publication.pravo.gov.ru/document/0001202609020026"
fi

echo "▶ 7. Адресная лента: GR-специалисту широкий новостной фон не нужен — исключаем Lenta.ru из опроса"
hub sources disable "$(src_id 'Lenta.ru')"

echo "▶ 8. Состояние источников"
hub sources list

echo "✓ GR happy path завершён. Дальше: uv run python -m src collect --watch --interval 3600"
echo "  (сайты регуляторов достаточно опрашивать раз в час)"
