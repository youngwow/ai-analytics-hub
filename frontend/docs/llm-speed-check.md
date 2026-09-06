# LLM speed check — 2026-09-06

The prompt now includes sentence boundaries computed from the exact normalized,
clipped document text. The model copies these coordinates instead of counting
characters to produce evidence offsets. The document text, model, output schema,
reasoning setting, and validation remain unchanged.

Three already processed documents were sent to the configured `glm-5.3-flash`
model with the candidate prompt. Their responses passed schema validation, and
the resulting evidence slices were inspected. Benchmark responses were not saved
as production cards.

| Document | Recorded original latency | Candidate latency | Original output tokens | Candidate output tokens |
| --- | ---: | ---: | ---: | ---: |
| 775 | 128.570 s | 12.857 s | 20,126 | 1,251 |
| 773 | 299.105 s | 73.391 s | 46,883 | 2,089 |
| 1116 | 76.410 s | 7.773 s | 12,531 | 1,256 |

This is a small comparison against historical calls, not a controlled throughput
benchmark. Provider queueing, load, caching, and model variation affect timing;
the observed 4–10× latency reductions are not a guarantee for the entire queue.

Processing concurrency is set to three, matching the user's Pro plan and the
[published Ollama concurrency limit](https://ollama.com/pricing).

Five backend regression cases cover Unicode character offsets, repeated
sentences, leading whitespace, empty documents, and clipped input. Backend unit
validation: 2,463 passed and 3 expected failures. Automated tests use no external
LLM calls.
