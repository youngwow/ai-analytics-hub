"""Frozen B3 outputs selected for the current B4 evaluation.

The A/B projections reuse the accepted v3 model analyses and expose unknowns
that were already stored but lost by the old release projection.  D1 is a
fresh run after the shared-critical-core policy.  Other scenarios are the
accepted v3 outputs and were unaffected by those policy changes.
"""

RUNS = {
    "A": "artifacts/b3/runs/v4_policy_A_projection/prediction.json",
    "B": "artifacts/b3/runs/v4_policy_B_projection/prediction.json",
    "D1_critical": "artifacts/b3/runs/v4_policy_D1_critical_hybrid/prediction.json",
    "D2_event_boundary": "artifacts/b3/runs/v3_final_D2_event_boundary_hybrid/prediction.json",
    "D3_new_npa": "artifacts/b3/runs/v3_final_D3_new_npa_hybrid/prediction.json",
    "D4_known_npa": "artifacts/b3/runs/v3_final_D4_known_npa_hybrid/prediction.json",
}
