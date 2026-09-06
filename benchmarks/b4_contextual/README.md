# B4 contextual product evaluation

B4 evaluates the **result**, not the agent architecture.  It uses the frozen
GS Labs context reconstruction, B3 originals, manually authored scenario gold
and the final B3 outputs.

```bash
.venv/bin/python benchmarks/b4_contextual/build_packet.py
.venv/bin/python benchmarks/b4_contextual/evaluate.py
```

- H1, H2 and H4 use deterministic counts.
- H3 and H5 use a manual source-to-result audit as the primary semantic gate.
  One historical GPT-OSS run is retained under `artifacts/b4/judges/debug/`
  only as a diagnostic; it predates the current projection and is not scored.
- No aggregate score is produced.  A critical miss, unsupported claim, false
  NPA status or lost independent source position cannot be averaged away.
- Human time, usability, trust and adoption remain untested until a real pilot.

The authoritative current semantic decision is
`artifacts/b4/B4_MANUAL_AUDIT.md`.  A second large model-judge run is not
required for this 18-object packet.

The benchmark data is synthetic and its gold was authored by the team before
the product runs.  Context relevance is therefore a team reconstruction, not
customer-labelled truth.

Старый LLM-judge и его summarizer перенесены в архив: найденный дефект
протокола не позволяет использовать их в текущем acceptance path.
