summarization
=============

Purpose
- Utilities and thin model/client glue to produce short meeting summaries from a
  transcript. Designed to be pluggable with local heuristics or an LLM-serving
  backend such as Ollama.

Key modules
- `ollama_client.py` — small wrapper to call Ollama; used for one-shot generation
  when available.
- `prompting.py` — builds prompts (e.g. one-sentence summary prompt) in a
  standardized way used by the server and client.
- `text.py` — safe display helpers: `first_sentence()`, `clamp_summary_one_sentence()`
  and whitespace normalization utilities to ensure output is UI-ready.
- `remote_client.py` — client wrapper to call server-side summarization endpoints
  from the Pi when offloading.
- `backfill_summaries.py` — helper for batch summarization/backfill workflows.

Integration
- The server uses `prompting.generate_one_shot` and `ollama_client` to produce
  final summaries for `/summarize`. The Pi can optionally call the server's
  summarization endpoints to offload expensive LLM calls.
