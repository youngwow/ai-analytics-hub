# B1 targeted gap audit

Проверяет только оставшиеся вопросы после замороженного `B1 v2`: state/backfill/cursor/recovery/no-truncation guardrails и эмпирическую форму сохранённых документов. Новую нагрузку на внешние сайты не создаёт; живую стабильность наследует только от явно названных трёх run.

```bash
.venv/bin/python benchmarks/b1_gap_audit/run.py
```

Результат находится в каталоге, названном в `runs/LATEST_GAP_AUDIT`.
