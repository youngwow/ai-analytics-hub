# B1 Collection benchmark

Независимая обвязка над контрактом адаптеров. Она не копирует внутреннюю архитектуру сборщика и не считает его реализацию эталоном.

```bash
.venv/bin/python benchmarks/b1_collection/run.py deterministic
.venv/bin/python benchmarks/b1_collection/run.py live --timeout 12 --workers 8 --fulltext-sample 1
.venv/bin/python benchmarks/b1_collection/run.py all
```

- `B1-D` воспроизводит сохранённые ответы реальных RSS/Atom, Telegram, sitemap, HTML и search-форматов.
- `B1-R-live-snapshot` опрашивает каждый источник из `sources.json`, включая выключенные, но не меняет рабочую БД.
- Внутри live-прогона отдельный изолированный `Collector` сохраняет и дотягивает ограниченную выборку полных текстов; рабочая БД не используется.
- JSON-отчёты сохраняются в `benchmarks/b1_collection/reports/`.

Live snapshot не является абсолютным recall: для него нужны замороженное семидневное окно, сохранение сырых ответов и независимая разметка объединённого пула.
