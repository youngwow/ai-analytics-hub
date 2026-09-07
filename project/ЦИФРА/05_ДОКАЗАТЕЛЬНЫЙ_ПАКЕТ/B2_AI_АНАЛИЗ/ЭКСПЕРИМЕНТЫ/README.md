# Эксперименты B2

> Tested system не видит gold. Prompt меняется только до freeze; свежий holdout не используется для повторного выбора красивой конфигурации.

## EXP-B2-01 — A1: один проход против двух

| Поле | One-pass | Two-pass |
|---|---:|---:|
| Validation role exact | 41,4% | 44,8% |
| Relevance macro-F1 | 58,8% | 55,8% |
| Importance macro-F1 | 83,2% | 60,1% |
| Wall time | 244 с | 642 с |

Менялось только число последовательных prompt-этапов; модель, материалы, контекст и схема результата были одинаковыми. Один дополнительный правильный role-set не перекрыл падение остальных метрик и рост задержки.

**Решение:** `A1 two-pass` убита, выбран one-pass.

Доказательства: [решение A1](../../../../../artifacts/b2/A1_DECISION.md), [validation decision](../../../../../artifacts/b2/B2_V2_DECISION.md), [one-pass run](../../../../../artifacts/b2/runs/dev_a1_onepass_a3_full_a4_off_v1/deterministic_report.json), [two-pass run](../../../../../artifacts/b2/runs/dev_a1_twopass_a3_full_a4_off_v1/deterministic_report.json).

## EXP-B2-02 — A5: выбор генеративной модели

| Модель | Худшая core-метрика validation | Решение |
|---|---:|---|
| `glm-5.3-flash:cloud` | 41,4% | **выбрана** |
| `deepseek-v4-flash:cloud` | 31,0% | не выбрана |
| `gpt-oss:120b-cloud` | 27,6% | не выбрана |

Модели сравнивались при одинаковом one-pass контуре и равном бюджете настройки. Это выбор для данного продукта и корзины, а не универсальный рейтинг моделей.

Доказательства: [B2 validation decision](../../../../../artifacts/b2/B2_V2_DECISION.md), [финальная конфигурация](../../../../../artifacts/b2/FINAL_CONFIG_V3.json).

## EXP-B2-03 — A4: отдельный AI-critic

| Поле | Без critic | С critic |
|---|---:|---:|
| Классификации на validation | baseline | не изменились |
| Дополнительные вызовы | 0 | 18 |
| Дополнительное время | 0 | 210 с |
| Review-policy critical coverage | 100% | 50% |

На раннем development-run critic изменил 8/17 карточек, исправил одну importance-ошибку и создал одну новую; итог не изменился, а добавилось 583,2 с и 53 519 output tokens.

**Решение:** `A4` убита. Schema, verbatim evidence и детерминированные safety gates остаются обязательными без второй LLM.

Доказательства: [решение A4](../../../../../artifacts/b2/A4_DECISION.md), [critic report](../../../../../artifacts/b2/runs/dev_a4_critic_v2/deterministic_report.json).

## EXP-B2-04 — обязательный контракт НПА

Это не искусственная гипотеза: система обязана различать identity, relation, stage и version.

| Проверка | Изолированный holdout после исправления |
|---|---:|
| Identity | 91,7% |
| Relation | 91,7% |
| Stage | 100% |

Первый общий NPA-контур уходил в timeout/default. Вместо скрытия провала контракт разделили на короткие проверяемые операции; спорные связи оставили на review.

Доказательства: [первый B2 result](../../../../../artifacts/b2/B2_RESULT.md), [NPA prediction](../../../../../artifacts/b2/runs/npa_split_capped_v3c_holdout/prediction.json), [NPA report](../../../../../artifacts/b2/runs/npa_split_capped_v3c_holdout/report.json).

## EXP-B2-05 — свежий post-freeze holdout

После заморозки prompt/config были добавлены 27 свежих материалов. Выполнены три одинаковых запуска.

| Метрика | Run 1 | Run 2 | Run 3 |
|---|---:|---:|---:|
| Failed model calls | 0 | 0 | 0 |
| Relevance | 9/19 | 8/19 | 9/19 |
| Importance | 13/18 | 12/18 | 13/18 |
| Roles exact | 10/19 | 11/19 | 11/19 |
| Связанные event pairs | 1/3 | 2/3 | 2/3 |
| Evidence | verbatim | verbatim | verbatim |

Смысловые знаменатели меньше 27, потому что спорные поля, по которым два blind-аудита gold не совпали, исключены, а не размечены удобным ответом.

**Решение:** техническая конфигурация передана в B3 только как review-first. Автономный выпуск не принят.

Доказательства:

- [итоговое решение](../../../../../artifacts/b2/B2_V4_DECISION.md);
- [общий scorecard](../../../../../artifacts/b2/HOLDOUT_SCORECARD_V4.json);
- [run 1](../../../../../artifacts/b2/runs/v4_holdout_glm_c4_final_r1/deterministic_report.json);
- [run 2](../../../../../artifacts/b2/runs/v4_holdout_glm_c4_final_r2/deterministic_report.json);
- [run 3](../../../../../artifacts/b2/runs/v4_holdout_glm_c4_final_r3/deterministic_report.json);
- [gold audit GLM-independent 1](../../../../../artifacts/b2/gold_audit_v4_holdout_deepseek-v4-flash.json);
- [gold audit GLM-independent 2](../../../../../artifacts/b2/gold_audit_v4_holdout_gpt-oss.json).

## Итог B2

`one-pass + GLM + deterministic gates + review-first`; two-pass и critic убиты, embeddings и research переданы в правильную E2E/scale-границу B3.
