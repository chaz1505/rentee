# Rentee end-to-end benchmark v1

This benchmark calls Rentee's real `POST /chat_stream` endpoint and follows one scripted tenant conversation: **Sofia - school bus, cats and teenage bedroom**. It captures SSE statuses, assistant text, citations, response IDs, timings, and Bubble state before and after the conversation. There is no automated scoring or LLM judge in v1.

## Test data and reset

The runner creates a dedicated Lead and linked Folio in the Bubble **development** database. Their IDs are reused from `tests/.autotest_state.json` while both records still exist.

Before every run, it:

1. removes all FolioItem references from the test Folio;
2. sets `newRecommendations` to `false`;
3. attempts to delete the old development FolioItems (a permission failure is warned about but is non-fatal after unlinking);
4. resets the Lead's `AIsearchtext` and `AIsearchsummary`.

The runner refuses any Bubble base URL that does not contain `/version-test/`. It always sends `"bubble_env": "development"` to `/chat_stream` and never falls back to live.

## Environment variables

- `BUBBLE_API_TOKEN` — required Bubble Data API token.
- `BUBBLE_DEV_BASE` — optional; defaults to `https://www.rentee.asia/version-test/api/1.1` and must contain `/version-test/`.
- `RENTEE_STREAM_URL` — optional; defaults to `https://rentee-2.onrender.com/chat_stream`.

## Run

From the repository root:

```bash
python tests/run_benchmark.py
```

On Render, the same command works when `BUBBLE_API_TOKEN` is already configured in the service environment.

Completed runs are stored as timestamped JSON files under `tests/results/`. v1 contains only the Sofia case. Recommendation scoring and an LLM judge will be added later.
