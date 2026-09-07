# B3 E2E benchmark

`B3 v2` — изолированная replay-среда для запуска любой реализации продукта. Она фиксирует внешний мир, входные данные, ожидаемые свойства результата и проверки, но не задаёт архитектуру агента.

## Состав

- `data/v2/context_gs_labs.json` — видимый постоянный контекст.
- `data/v2/initial_states.json` — видимое состояние до начала каждого сценария.
- `data/v2/timeline.jsonl` — 58 входов с виртуальным временем и transport payload.
- `data/v2/prediction.schema.json` — контракт ответа системы.
- `data/v2/ground_truth.jsonl`, `object_truth.jsonl`, `npa_truth.jsonl`, `expected_transitions.json` — закрытый эталон.
- `build_v2.py` — детерминированная сборка.
- `validate_v2.py` — целостность, покрытие, состояния, утечки и checksums.
- `prepare_input.py` — безопасный пакет для агента.
- `replay_server.py` — локальная HTTP-обёртка потоков.
- `evaluate.py` — точные структурные метрики и пакет для смыслового judge.
- `validate_judge.py` — не даёт принять неполный ответ смыслового judge за результат.
- `run_benchmark.py` — архитектурно нейтральный запуск любой внешней команды по контракту файлов.
- `evaluate_human.py` — проверка полноты и сводка замеров `VALUE/H2–H5` без автоматического выдумывания вывода.
- `evaluate_acceptance.py` — pass/fail восьми обязательных операций с обязательным evidence.
- `data/v2/acceptance_result.schema.json` — контракт evidence: существующий артефакт/URL плюс наблюдение проверяющего.
- `make_oracle_prediction.py` — проверка самого scorer; продукту этот файл и результат не передаются.
- `scale/` и `data/scale_v1/` — отдельный `B3-SCALE`: растущий банк `100 / 1 000 / 10 000`, события и известные НПА, `full_scan` против `embedding top-20 + той же LLM`.
- `research/` и `data/a2_v1/` — парный живой тест `A2`: реальные B1-сигналы без/с целевым Tavily-доресерчем и контроль, где поиск не должен запускаться.
- `npa_lifecycle/` — детерминированный `EXP-B3-06`: принятый НПА с будущей датой остаётся на мониторинге, в дату действия становится доступен для явной архивации, история версий сохраняется.

## Быстрый запуск

```bash
python benchmarks/b3_e2e/build_v2.py
python benchmarks/b3_e2e/validate_v2.py
python benchmarks/b3_e2e/prepare_input.py --scenario A --mode hybrid --output /tmp/b3-a.json
python benchmarks/b3_e2e/replay_server.py --port 8787
```

Агент должен вернуть JSON по `prediction.schema.json`. Проверка результата:

```bash
python benchmarks/b3_e2e/evaluate.py prediction.json --version v2 --report report.json --judge-packet judge-packet.json
# После независимого смыслового judge:
python benchmarks/b3_e2e/validate_judge.py judge-response.json --judge-packet judge-packet.json --report judge-validation.json
```

Или пакетный content-прогон любой реализации одной командой:

```bash
python benchmarks/b3_e2e/run_benchmark.py --scenario A --mode hybrid --output-dir runs/b3-a -- your-agent --input {input} --output {output}
```

Вместо placeholders реализация может читать пути из `B3_INPUT` и `B3_OUTPUT`. Benchmark не знает, является ли она одним агентом, графом, пайплайном или обычной программой.

Этот runner проверяет обработку одного подготовленного пакета и измеряет wall time всей команды. Он не имитирует поступление данных по часам. Временной интеграционный E2E запускается через `replay_server.py` и продуктовые коннекторы; его нельзя подменять пакетным прогоном.

Команда запускается из отдельной папки конкретного прогона, где лежит только пакет ввода и создаваемые результаты. Это защищает от случайного чтения эталона. Это не security-sandbox против намеренно вредоносного процесса: агенту с произвольным доступом ко всей файловой системе нельзя одновременно давать директорию `data/v2`.

`report.json` содержит только точные метрики. `judge-packet.json` передаётся независимому judge или человеку для проверки смысла, атрибуции мнений, достаточности саммари и неподтверждённых утверждений. Пользовательское время для `VALUE/H2/H4/H5` записывается отдельно по `observation.schema.json`.
