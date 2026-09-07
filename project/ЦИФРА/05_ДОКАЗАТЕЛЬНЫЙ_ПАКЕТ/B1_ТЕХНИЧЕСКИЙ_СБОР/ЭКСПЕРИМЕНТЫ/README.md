# Эксперименты B1

> Меняется только техническое поведение ingestion. Смысл новости, роль и приоритет здесь не оцениваются.

## EXP-B1-C1 — точный контракт адаптеров

| Поле | Содержание |
|---|---|
| Вопрос | Возвращает ли каждый адаптер ровно ожидаемый набор документов и обязательных полей? |
| Вход | Замороженные RSS, Atom, Telegram, HTML, sitemap, search и fulltext fixtures |
| Истина | Exact manifest, зафиксированный до прогона |
| Измерение | external IDs, unknown IDs, обязательные поля, attachments, cursor/validator |
| Результат | 8/8 сценариев; recall 100%; false positives 0; обязательные поля 100% |
| Решение | Контракт адаптеров принят |

Доказательства: [contract.json](../../../../../benchmarks/b1_proof/contract.json), [runner](../../../../../benchmarks/b1_proof/run.py), [общий report](../../../../../benchmarks/b1_proof/runs/20260906T015442Z_proof/report.json).

## EXP-B1-C2 — состояние, повторы и отказы

| Поле | Содержание |
|---|---|
| Вопрос | Теряет ли система данные при повторе, timeout или частичном отказе? |
| Сценарии | `200→304`, второй запуск, Telegram cursor, timeout→recovery, `403/429/500`, broken feed, child-sitemap failure |
| Инвариант | Ошибка одного источника не останавливает соседние; recovery не теряет доступный fixture |
| Результат | Все критичные сценарии прошли; повтор создал 0 новых документов |
| Найденный дефект | Дочерний `500` sitemap раньше маскировался как `ok` |
| Сдвиг | Сбой теперь даёт `partial` + warning и виден оператору |

Доказательства: [общий report](../../../../../benchmarks/b1_proof/runs/20260906T015442Z_proof/report.json), [тесты Collector](../../../../../tests/unit/test_collector.py).

## EXP-B1-C3 — replay и параллельность

| Вариант | Wall time | Fingerprint | Итог |
|---:|---:|---|---|
| `concurrency=1` | 1609 мс | совпал | корректно, медленнее |
| `concurrency=8` | 816 мс | совпал | **выбран** |
| `concurrency=24` | 780 мс | совпал | быстрее менее чем на 10%, лишняя параллельность |

Вход — одинаковые сохранённые байты; 33 документа — прозрачный benchmark sample по три материала на источник, а не лимит продукта. Replay misses: 0.

Доказательства: [общий report](../../../../../benchmarks/b1_proof/runs/20260906T015442Z_proof/report.json), [B1 technical runner](../../../../../benchmarks/b1_technical/run_full.py).

## EXP-B1-C4 — live compatibility

| Поле | Результат |
|---|---:|
| Активные источники | 44 |
| Проходы | 3 |
| Source attempts | 132 |
| `ok / partial / failed` | 129 / 3 / 0 |
| Сохранённые документы | 99 |
| Fulltext | 90/90 |
| Длительность прохода | 16,0–17,3 с |
| Credential leaks | 0 |

Три `partial` относятся к одному дочернему sitemap РФРИТ. Он не скрыт и не превращён в общий `PASS` методом усреднения. Девять выключенных входов проверены отдельно и не влияют на verdict активной конфигурации.

Доказательства:

- [active run 1](../../../../../benchmarks/b1_proof/runs/20260906T015446Z_active_1/live_report.json);
- [active run 2](../../../../../benchmarks/b1_proof/runs/20260906T015502Z_active_2/live_report.json);
- [active run 3](../../../../../benchmarks/b1_proof/runs/20260906T015519Z_active_3/live_report.json);
- [registered diagnostic](../../../../../benchmarks/b1_proof/runs/20260906T015536Z_registered-disabled_1/live_report.json).

## Итог B1

`C1 PASS + C2 PASS + C3 PASS + C4 PASS с известным partial → ingestion допущен в MVP`.
