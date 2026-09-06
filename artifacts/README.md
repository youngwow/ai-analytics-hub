# Результаты прогонов

Эксперту достаточно четырёх точек входа:

1. [`FINAL_VERIFICATION.md`](./FINAL_VERIFICATION.md) — последняя техническая проверка репозитория.
2. [`b2/B2_RESULT.md`](./b2/B2_RESULT.md) — выбор AI-конфигурации и честный общий `FAIL` первого цикла.
3. [`b3/B3_EVIDENCE.md`](./b3/B3_EVIDENCE.md) — итог полного E2E, решения и ограничения.
4. [`demo/gs_labs_demo.db`](./demo/gs_labs_demo.db) — изменяемая копия данных для live-demo.
5. [`b3/HUMAN_PILOT_PROTOCOL_V1.md`](./b3/HUMAN_PILOT_PROTOCOL_V1.md) — единственный оставшийся пользовательский gate: Excel против MVP.

В `b2/runs`, `b3/final_protocol` и `b3/final_rerun` лежат воспроизводимые машинные результаты. Для исправленных сценариев авторитетен `final_rerun`; для `search` и `D1/D2` — `final_protocol`. Старые промежуточные B3-прогоны и тяжёлые HTTP-payload B1 вынесены из репозитория в локальный архив, но сохранены в полном бэкапе проекта.
