# A4 — решение по development

> **Решение:** для MVP выбран `without_critic`; отдельный AI-critic не принят.

Control не генерировался заново: A4 применён к сохранённым карточкам one-pass из
`dev_a1_onepass_a3_full_a4_off_v1`. Первый запуск выявил нарушение JSON-контракта; после усиления
схемы и добавления чекпоинтов выполнен сравнимый `dev_a4_critic_v2`.

| Метрика | Без critic | С critic |
|---|---:|---:|
| Relevance accuracy | 82.4% | 82.4% |
| Importance accuracy | 64.7% | 64.7% |
| Critical/escalate accuracy | 100% | 100% |
| Roles exact accuracy | 29.4% | 29.4% |
| Verbatim evidence | 100% | 100% |
| Дополнительные вызовы | 0 | 15 |
| Дополнительное wall time | 0 | 583.2 с |
| Выходные токены critic | 0 | 53 519 |

Critic изменил 8 из 17 карточек: одну ошибку importance исправил и одну внёс, поэтому итоговая
точность не изменилась. Дополнительная задержка и нестабильность не окупаются. Сам контракт critic
и ограниченные повторы сохранены как защитный механизм для будущих точечных сценариев, но в main flow не включены.

Артефакты: `runs/dev_a4_critic_v2/prediction.json` и `deterministic_report.json`.
