# B3 / A2 — targeted research

This experiment checks one narrow architecture decision: whether a reasoning
step with Tavily adds decision-relevant evidence to an important, incomplete
signal compared with analysing only the captured primary material.

It deliberately does **not** score product value or user satisfaction.  The
live search run is retained as evidence, while `cases.json` and the original B1
documents make the input auditable.

```bash
.venv/bin/python benchmarks/b3_e2e/research/build_cases.py
.venv/bin/python benchmarks/b3_e2e/research/validate_cases.py
.venv/bin/python benchmarks/b3_e2e/research/run_research.py \
  --output artifacts/b3/a2_research/run.json
```

Cases:

- `A2-news`: an incomplete market signal where corroboration and the status of
  the underlying initiative can change the interpretation;
- `A2-NPA`: an NPA candidate where the official document, stage and obligations
  matter more than press commentary;
- `A2-skip`: a low-importance complete signal proving that research is gated
  rather than attached to every item.

Acceptance is count-based and auditable: the expected gate fires, every accepted
claim contains a verbatim quote from a retained search result, and the run shows
whether the stated evidence gaps were actually resolved.  A human-readable
decision is written only after the returned sources and claims are audited.
