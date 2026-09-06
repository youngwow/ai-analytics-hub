# B1 technical benchmark

```bash
.venv/bin/python benchmarks/b1_technical/run_full.py \
  --sample-per-source 3 \
  --concurrency 8 \
  --load-levels 1,8,24 \
  --load-repeats 5
```

Прогон сохраняет:

- каждый сырой HTTP-ответ в content-addressed `raw/sha256/`;
- обезличенный индекс запросов и ответов `captures.jsonl`;
- отдельные читаемые Tavily request/response в `search_artifacts/`;
- документы после полного Collector-прохода `documents.jsonl`;
- результаты по источникам `source_results.json`;
- полный отчёт, нагрузочные метрики и рейтинг методов `report.json`.

Authorization, Cookie, Set-Cookie и API-ключи в артефакты не записываются.

Нагрузочный режим воспроизводит сохранённые байты локально и измеряет адаптеры,
нормализацию и ingestion-код. Он намеренно не создаёт искусственную DDoS-нагрузку
на внешние сайты. Устойчивость внешнего контура проверяется несколькими отдельными
живыми проходами, которые можно свести командой:

```bash
.venv/bin/python benchmarks/b1_technical/summarize_runs.py \
  benchmarks/b1_technical/runs/<run-1> \
  benchmarks/b1_technical/runs/<run-2> \
  benchmarks/b1_technical/runs/<run-3> \
  --output benchmarks/b1_technical/runs/summary_3_runs.json
```

`B1` не определяет смысловую релевантность материалов для GS Labs. Это задача
AI-бенчмарка `B2` и продуктового `B3`.
