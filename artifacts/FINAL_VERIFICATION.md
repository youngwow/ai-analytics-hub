# Финальная техническая проверка

**Дата:** 7 сентября 2026
**Область:** review-first MVP, его контейнерная поставка и сохранённые benchmark-артефакты.

| Проверка | Результат |
|---|---|
| Полный test suite | `1171 passed`, одна внешняя необязательная warning о `PySocks` |
| Ruff | `PASS`, ошибок нет |
| Docker build | `PASS`, образ `gs-labs-radar:local` собран из текущего дерева |
| Чистый Docker smoke | `PASS`, отдельный read-only контейнер с пустой БД: `/` и `/api/pilot/status` вернули HTTP `200` |
| Docker isolation | `.env` не встроен в образ; локальные БД и Telegram-сессия остаются во внешних mount-каталогах |
| B3 scorecard против исходных JSON | `PASS` |
| B4 frozen packet | `18` объектов; manifest и детерминированный scorer прошли |
| B4 ручной аудит | `18/18` объектов сохранили обязательное содержание; строгая H4 убита |
| JSON-артефакты репозитория | `PASS` |
| Локальные Markdown-ссылки | `PASS` |
| Demo SQLite | `PRAGMA integrity_check = ok` |
| Web/API smoke | очередь, карточка/evidence, проверяемый отсев, НПА и overview открываются; console errors отсутствуют |
| Секреты | `.env` игнорируется; реальные ключи в содержимом репозитория не найдены |
| OpenRouter embeddings | HTTP `200`, `google/gemini-embedding-001`, 3072 измерения |
| Pilot workbook | 6 листов, 22 материала в A и B, 8 строк cross-over, ошибок формул нет |
| Pilot SQLite A/B | `integrity_check = ok`, по 20 уникальных материалов, отсутствующих связей с исходником нет |

Демонстрационная база содержит 22 подготовленных материала, 14 сигналов, 7 событий и один НПА с историей. Локальный запуск: `make demo`; воспроизводимый контейнерный запуск: `docker compose up --build -d`.

Полный E2E-результат и границы доказательства находятся в [`b3/B3_EVIDENCE.md`](./b3/B3_EVIDENCE.md), решения по всем гипотезам — в [`EVIDENCE_PACK.md`](./EVIDENCE_PACK.md), а контекстная продуктовая валидация — в B4.
