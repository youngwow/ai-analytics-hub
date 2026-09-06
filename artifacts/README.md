# Результаты прогонов

Эксперту достаточно пяти точек входа:

1. [`FINAL_VERIFICATION.md`](./FINAL_VERIFICATION.md) — последняя техническая проверка репозитория.
2. [`b2/B2_RESULT.md`](./b2/B2_RESULT.md) — выбор AI-конфигурации и честный общий `FAIL` первого цикла.
3. [`b3/B3_EVIDENCE.md`](./b3/B3_EVIDENCE.md) — итог полного E2E, решения и ограничения.
4. [`demo/gs_labs_demo.db`](./demo/gs_labs_demo.db) — изменяемая копия данных для live-demo.
5. [`b3/HUMAN_PILOT_PROTOCOL_V1.md`](./b3/HUMAN_PILOT_PROTOCOL_V1.md) и [`b3/PILOT_EXCEL_BASELINE_V1.xlsx`](./b3/PILOT_EXCEL_BASELINE_V1.xlsx) — готовый пользовательский gate: Excel против MVP.

В активных папках оставлены только выбранные конфигурации, финальные повторы и прогоны, на которые опираются решения. Черновые, невалидные и заменённые прогоны сохранены в [`_archive/benchmark-history`](../_archive/benchmark-history/).

Для B3 авторитетен `final_rerun` для исправленных сценариев, а `final_protocol` — для `search` и `D1/D2`.
