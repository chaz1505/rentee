# Rentee end-to-end benchmark v1

This benchmark calls Rentee's real `POST /chat_stream` endpoint and follows one scripted tenant conversation: **Sofia - school bus, cats and teenage bedroom**. It captures SSE statuses, assistant text, citations, response IDs, timings, and Bubble state before and after the conversation, then runs deterministic and qualitative evaluation.

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
- `BENCHMARK_API_KEY` — required to use the protected HTTP trigger and status endpoints.
- `GITHUB_RESULTS_TOKEN` — optional; publishes completed artifacts to GitHub when present.

## Run

### Preferred: protected HTTP trigger

Configure `BENCHMARK_API_KEY` on Render, then start a background run:

```bash
curl -X POST \
  -H "X-Benchmark-Key: <key>" \
  https://rentee-2.onrender.com/admin/run_benchmark
```

Monitor progress at **Render → Rentee service → Logs**. Every benchmark line is flushed to standard output with a `[BENCHMARK]` prefix.

Query the current process-local status with:

```bash
curl \
  -H "X-Benchmark-Key: <key>" \
  https://rentee-2.onrender.com/admin/benchmark_status
```

Only one benchmark can run per service process. Status is ephemeral and resets when Render restarts; generated GitHub artifacts are permanent.

### Fallback: command line

From the repository root:

```bash
python tests/run_benchmark.py
```

On Render, the same command works when `BUBBLE_API_TOKEN` is already configured in the service environment.

After the conversation completes, deterministic evaluation checks latency, stream integrity, unsupported promises, excessive and repeated questions, current-recommendation behaviour, historical-recommendation language, and configured preference persistence. One `gpt-5-mini` evaluator call then assesses genuinely qualitative behaviour: conversation intelligence, recommendation reasoning, adaptiveness, question quality, and decision progress. Set `RENTEE_EVALUATOR_MODEL` to override that model.

The evaluator compares the run with the most recent earlier raw result for the same case. It reports latency changes and changes in the major violation counts; qualitative scores are compared when the prior evaluation file exists.

Each completed run generates four files:

```text
tests/results/<case>_<timestamp>.json
tests/results/<case>_<timestamp>_evaluation.json
tests/results/<case>_<timestamp>_evaluation.md
tests/results/<case>_<timestamp>_fix_prompt.md
```

The raw JSON contains the transcript, timings, and state. The evaluation JSON is the structured machine-readable assessment. The `_evaluation.md` report is the main human-readable artifact and includes performance, comparisons, strengths, problems, scores, priorities, and the full verbatim conversation. The `_fix_prompt.md` file is a ready-to-paste implementation task for Codex.

Normal review workflow:

```text
Trigger benchmark
→ watch progress in Render Logs
→ open GitHub tests/results/
→ open the latest _evaluation.md
→ review performance and the full conversation
→ open _fix_prompt.md if changes are needed
```

After Codex makes a fix, rerun `python tests/run_benchmark.py`. The next evaluation automatically compares the new run with the preceding one. v1 contains only the Sofia case.

## GitHub artifact publishing

When `GITHUB_RESULTS_TOKEN` is configured, each run automatically publishes its four generated artifacts to `chaz1505/rentee` on `main`, under `tests/results/`. Publishing uses the GitHub Contents API and does not depend on Render having a usable Git checkout or push remote.

Run normally:

```bash
python tests/run_benchmark.py
```

If the token is absent, the benchmark still completes and the artifacts remain local on Render. To explicitly disable publishing even when the token exists:

```bash
BENCHMARK_SKIP_GITHUB=true python tests/run_benchmark.py
```

The token is used only in the GitHub API authorization header. It requires Contents write access to `chaz1505/rentee`. Before publishing, the helper scans all four artifacts for configured GitHub, Bubble, and OpenAI secret values and refuses the entire run if one is found. `tests/.autotest_state.json` is never eligible for publication.

Publishing failures never change the benchmark result. Partial artifacts from failed Rentee conversations are evaluated, turned into a fix prompt, and published through the same flow when credentials are available.
