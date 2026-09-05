# B1 Collection Proof

Канонический технический benchmark ingestion-контура. Он не оценивает смысловую релевантность и AI-качество.

```bash
.venv/bin/python benchmarks/b1_proof/run.py --live --registered-diagnostic
```

Слои:

- `C1`: exact fixtures и fulltext;
- `C2`: state, faults, recovery и isolation;
- `C3`: полный Collector на одинаковых сохранённых байтах при `1/8/24`;
- `C4`: три умеренных live-прохода активных входов и отдельная диагностика выключенных.

Контракт зафиксирован в `contract.json`, его hash — в `CONTRACT_SHA256`. Итоговый отчёт находится в каталоге, указанном файлом `runs/LATEST_PROOF`.
