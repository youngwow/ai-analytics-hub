# EXP-B3-06 — terminal NPA lifecycle

This deterministic experiment closes the terminal part of the NPA lifecycle
that D3/D4 did not execute:

`public discussion → revised draft → adopted with future effective date → archive eligible`.

The case owner explicitly said an NPA is monitored until it reaches “adopted,
effective from DATE”; only after that may it be archived. The experiment checks
that:

1. an adopted act remains tracked before its effective date;
2. it becomes archive-eligible on that date;
3. archive is explicit and idempotent;
4. all prior versions remain stored;
5. a neighbouring NPA is not affected.

No LLM is used: AI extraction of identity/stage is already covered by B2 and
B3-D3/D4. This experiment isolates the deterministic product lifecycle after
the official state has been resolved.

Run:

```bash
.venv/bin/python benchmarks/b3_e2e/npa_lifecycle/run.py \
  --output artifacts/b3/npa_lifecycle/EXP_B3_06_RESULT.json
```
