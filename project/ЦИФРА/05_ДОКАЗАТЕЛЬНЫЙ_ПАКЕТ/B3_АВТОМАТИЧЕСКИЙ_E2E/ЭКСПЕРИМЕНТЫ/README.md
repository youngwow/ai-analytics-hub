# Эксперименты B3

> B3 использует синтетический, но сценарно правдоподобный мир. Сценарии специально покрывают продуктовые риски, однако gold невидим агенту и не подмешан в runtime.

## EXP-B3-01 — полный автоматический контур

| Поле | Содержание |
|---|---|
| Вход | 58 материалов, два обычных окна и четыре диагностических сценария, initial state событий/НПА |
| Tested system | Тот же `ProductAgentRuntime`, product contracts и SQLite, что в приложении |
| Выход | decisions, events, NPA versions, review set, DraftDigest, delivery plan |
| Проверка | exact labels, event pairs, NPA lifecycle, evidence grounding, schema/contract |
| Результат | relevance 55/58; importance 43/58; roles 42/58 до policy fix; event pairs 34/34; contract failures 0 |
| Решение | Контур принят только как review-first |

Доказательства: [scorecard](../../../../../artifacts/b3/B3_SCORECARD_V3.json), [E2E evidence](../../../../../artifacts/b3/B3_EVIDENCE.md), [runner](../../../../../benchmarks/b3_e2e/run_benchmark.py), [product agent](../../../../../benchmarks/b3_e2e/product_agent.py).

## EXP-B3-02 — H1: fixed / search / hybrid

Одинаковые временные окна запускались в трёх режимах. Значимость считалась по заранее заданным объектам Context Truth; шум — как полученные материалы вне этих объектов.

| Окно | Fixed | Search-only | Hybrid | Шум F / S / H |
|---|---:|---:|---:|---:|
| A | 6/7 | 3/7 | 7/7 | 7 / 1 / 8 |
| B | 6/6 | 2/6 | 6/6 | 7 / 1 / 8 |

**Решение:** исходная гипотеза постоянного гибрида убита — прирост оказался нестабильным, шум вырос в обоих окнах. Search-only также убит как замена источникам. Архитектура: fixed baseline + targeted search при явном пробеле.

Доказательства: [B3 scorecard](../../../../../artifacts/b3/B3_SCORECARD_V3.json), [A fixed run](../../../../../artifacts/b3/runs/v3_final_A_fixed/deterministic_report.json), [A search run](../../../../../artifacts/b3/runs/v3_final_A_search/deterministic_report.json), [A hybrid run](../../../../../artifacts/b3/runs/v3_final_A_hybrid/deterministic_report.json), [B fixed run](../../../../../artifacts/b3/runs/v3_final_B_fixed/deterministic_report.json), [B search run](../../../../../artifacts/b3/runs/v3_final_B_search/deterministic_report.json), [B hybrid run](../../../../../artifacts/b3/runs/v3_final_B_hybrid/deterministic_report.json).

## EXP-B3-03 — A2: без research / targeted research

| Кейс | Полезные новые факты | Время исследовательской части | Риск |
|---|---:|---:|---|
| News | 5/7 прошли ручной аудит | 76,3 с | смысловые расширения |
| NPA | 11/13 прошли ручной аудит | 201,4 с | официальный статус нельзя подменять Tavily |
| Контроль | research не запущен | 0 внешних вызовов | корректный skip |

Четыре из двадцати новых утверждений расширяли смысл цитаты. Следовательно, полезность есть, но исходное обещание «без неприемлемой задержки и ошибок» не прошло.

**Решение:** A2 в исходной форме убита. Research оставлен асинхронным только для `high/critical` с конкретным пробелом, лимитом запросов и обязательным review.

Доказательства: [решение A2](../../../../../artifacts/b3/a2_research/A2_DECISION.md), [ручной аудит](../../../../../artifacts/b3/a2_research/A2_GOLD_AUDIT.md), [финальный run](../../../../../artifacts/b3/a2_research/run_final.json).

## EXP-B3-04 — A3-SCALE: full scan / embeddings top-20

| Проверка | Результат |
|---|---:|
| Банки | 100 / 1 000 / 10 000 |
| Event candidate recall@20 | 4/4 |
| NPA candidate recall@20 | 3/3 |
| Старое event-обновление | найдено около позиции 4096 |
| Старое NPA-обновление | найдено около позиции 9001 |
| Full scan 8996 events | 1 200 129 tokens > окно 1 048 576; HTTP 400 |
| Fresh holdout resolver | object 4/4; relation 3/4 |
| Второй verifier | тот же результат; median latency 3492→6199 мс |

**Решение:** A3 подтверждена в части candidate retrieval. Gemini embeddings top-20, затем GLM используются всегда: банк начинает расти с первого дня. Full scan оставлен только benchmark-контролем и аварийным fallback. Второй verifier убит.

Доказательства: [решение](../../../../../artifacts/b3/B3_SCALE_DECISION.md), [adjudicated scorecard](../../../../../artifacts/b3/B3_SCALE_SCORECARD_ADJUDICATED.json), [fresh holdout](../../../../../artifacts/b3/B3_SCALE_HOLDOUT_SCORECARD.json), [gold audit](../../../../../artifacts/b3/B3_SCALE_GOLD_AUDIT.md).

## EXP-B3-05 — критичное общее ядро

B4 обнаружил, что строгая ролевая метка могла скрыть критичный сигнал. Было введено системное правило: `critical/urgent → PR + GR + HEAD`. После этого свежий D1-run дал точные роли 3/3 и сохранил критичный объект во всех трёх представлениях.

**Решение:** исправление принято как продуктовая политика, а не как case-specific эвристика.

Доказательства: [D1 prediction](../../../../../artifacts/b3/runs/v4_policy_D1_critical_hybrid/prediction.json), [execution](../../../../../artifacts/b3/runs/v4_policy_D1_critical_hybrid/execution.json), [реализованное правило](../../../../../src/product/contracts.py).

## EXP-B3-06 — терминальный жизненный цикл НПА

Этот сценарий добавлен после финального аудита покрытия. D3 проверял создание НПА, D4 — обновление известного объекта до `adopted`, но не исполнял прямое требование кейсодателя: НПА остаётся на мониторинге до состояния «принято, действует с даты», после чего его можно архивировать.

| Инвариант | Результат |
|---|---|
| До `effective_from` НПА остаётся активным | PASS |
| До даты архивирование запрещено | PASS |
| В дату действия появляется право архивации | PASS |
| Архивация выполняется явно | PASS |
| Повторная архивация не создаёт второе действие | PASS |
| История `public_discussion → revised_draft → adopted` сохранена | 3/3 версии |
| Соседний похожий НПА не изменён | PASS |
| Событие архивации трассируется | ровно 1 audit event |
| Итог | **9/9, PASS** |

**Решение:** терминальная часть lifecycle принята. Архив — это выключение активного мониторинга, а не удаление НПА и его версий. LLM здесь намеренно не используется: извлечение identity/stage уже проверено B2 и D3/D4, а этот EXP изолирует детерминированное правило времени и хранения.

Доказательства: [описание и runner](../../../../../benchmarks/b3_e2e/npa_lifecycle/README.md), [run.py](../../../../../benchmarks/b3_e2e/npa_lifecycle/run.py), [сохранённый результат](../../../../../artifacts/b3/npa_lifecycle/EXP_B3_06_RESULT.json), [unit regression](../../../../../tests/unit/test_product_store.py), [слова кейсодателя](../../../ЧАТ.md).

## Итог B3

`fixed sources → one-pass GLM → explicit unknowns → targeted async research → embeddings top-20 → event/NPA lifecycle → risk-based review → DraftDigest`.
