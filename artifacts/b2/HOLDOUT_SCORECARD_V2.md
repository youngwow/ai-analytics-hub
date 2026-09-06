# B2 holdout — сводка повторов

**Вердикт: FAIL**. Повторов: 3.

| Метрика | Среднее | Минимум | Максимум |
|---|---:|---:|---:|
| `relevance_accuracy` | 0.8333 | 0.7500 | 0.9000 |
| `importance_accuracy` | 0.7667 | 0.7000 | 0.8500 |
| `critical_gate_accuracy` | 0.9000 | 0.9000 | 0.9000 |
| `roles_exact_accuracy` | 0.5833 | 0.5000 | 0.6500 |
| `event_pair_f1` | 0.8413 | 0.6667 | 1.0000 |
| `npa_identity_accuracy` | 0.2500 | 0.2500 | 0.2500 |
| `npa_relation_accuracy` | 0.2500 | 0.2500 | 0.2500 |
| `npa_stage_accuracy` | 0.0000 | 0.0000 | 0.0000 |
| `review_workload_rate` | 0.8500 | 0.8500 | 0.8500 |
| `review_label_error_recall` | 1.0000 | 1.0000 | 1.0000 |
| `review_critical_recall` | 1.0000 | 1.0000 | 1.0000 |
| `verbatim_evidence_rate` | 1.0000 | 1.0000 | 1.0000 |
| `wall_seconds` | 617.0510 | 552.1105 | 658.6794 |
| `attempted_calls` | 24.0000 | 23.0000 | 25.0000 |
| `failed_calls` | 2.0000 | 1.0000 | 3.0000 |

## Блокирующие причины

- provider failures occurred in every frozen end-to-end result
- NPA identity linking is below the acceptance orientation
- NPA stage tracking is below the acceptance orientation

Safety и отказоустойчивость оцениваются по худшему повтору, а не по среднему.
