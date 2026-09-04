#!/usr/bin/env bash
# Happy path для PR-менеджера: репутация — упоминания GS Labs / «Триколор», конкуренты, отраслевые новости.
# Запуск из корня репозитория: bash scripts/happy_path_pr.sh
# Повторный запуск безопасен: источники не дублируются, уже сохранённые материалы пропускаются.
set -euo pipefail
cd "$(dirname "$0")/.."

hub() { uv run python -m src "$@"; }
have_key() { [ -n "${TAVILY_API:-}" ] || grep -Eq '^TAVILY_API=.+' .env 2>/dev/null; }

echo "▶ 1. Пул источников из context/sources_for_company.md: СМИ, регуляторы, Telegram-зеркала"
hub sources seed

echo "▶ 2. Первый сбор: RSS, sitemap, t.me/s/ (окно 72 ч, до 50 материалов с источника)"
hub collect

echo "▶ 3. Что пришло: последние материалы по всем источникам"
hub docs --limit 15

if have_key; then
  echo "▶ 4. Упоминания компании за неделю по отраслевым СМИ (Tavily): попадания и «Сводка» ложатся в documents,"
  echo "     запрос сохраняется как источник — collect будет опрашивать его дальше"
  hub search "GS Labs OR Триколор OR StingrayTV" --domains @media --days 7 --save \
    --name "Поиск: GS Labs / Триколор за неделю"

  echo "▶ 5. Разовый запрос по продуктовой теме за месяц (источник остаётся выключенным, материалы сохранены)"
  hub search "система условного доступа CAS DRM спутниковое телевидение" --domains @media --days 30
else
  echo "▶ 4–5. Поиск через Tavily пропущен: задайте TAVILY_API в .env (cp .env.example .env)"
fi

echo "▶ 6. Состояние источников: что работает, где ошибка, сколько собрано"
hub sources list

echo "✓ PR happy path завершён. Дальше: uv run python -m src collect --watch --interval 900"
echo "  (опрос каждые 15 минут, включая сохранённые поисковые запросы)"
