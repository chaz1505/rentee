# Rentee end-to-end benchmark v1

This benchmark calls Rentee's real `POST /chat_stream` endpoint and follows one scripted tenant conversation: **Sofia - school bus, cats and teenage bedroom**. It captures SSE statuses, assistant text, citations, response IDs, timings, and Bubble state before and after the conversation, then runs deterministic and qualitative evaluation. Each execution is stored as one Bubble `BenchmarkRun`.

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

Only one benchmark can run per service process. Status is ephemeral and resets when Render restarts; Bubble `BenchmarkRun` is the durable execution history.

### Fallback: command line

From the repository root:

```bash
python tests/run_benchmark.py
```

On Render, the same command works when `BUBBLE_API_TOKEN` is already configured in the service environment.

After the conversation completes, deterministic evaluation checks latency, stream integrity, unsupported promises, excessive and repeated questions, current-recommendation behaviour, historical-recommendation language, and configured preference persistence. One `gpt-5-mini` evaluator call then assesses genuinely qualitative behaviour: conversation intelligence, recommendation reasoning, adaptiveness, question quality, and decision progress. Set `RENTEE_EVALUATOR_MODEL` to override that model.

The evaluator first preserves comparison with an available local earlier result for the same case and environment, and can use the latest prior environment-specific Bubble `BenchmarkRun` when local history is unavailable. Failure to find history never prevents persistence.

Each completed run generates four files:

```text
tests/results/<case>_<environment>_<timestamp>.json
tests/results/<case>_<environment>_<timestamp>_evaluation.json
tests/results/<case>_<environment>_<timestamp>_evaluation.md
tests/results/<case>_<environment>_<timestamp>_fix_prompt.md
```

The raw JSON contains the transcript, timings, and state. The evaluation JSON is the structured machine-readable assessment. The `_evaluation.md` report is the main human-readable artifact and includes performance, comparisons, strengths, problems, scores, priorities, and the full verbatim conversation. The `_fix_prompt.md` file is a ready-to-paste implementation task for Codex.

Normal review workflow:

```text
Trigger benchmark
→ watch Render Logs if desired
→ BenchmarkRun saved automatically
→ open Rentee AI Testing admin page
→ read evaluationMarkdown and review the full conversation
→ copy fixPrompt into Codex
```

After Codex makes a fix, rerun `python tests/run_benchmark.py`. The next evaluation automatically compares the new run with the preceding one. v1 contains only the Sofia case.

## Result storage

GitHub stores benchmark code, benchmark cases, and evaluator code. It is not used as the durable store for new execution results and `GITHUB_RESULTS_TOKEN` is not required.

Bubble `BenchmarkRun` stores benchmark history, raw results, structured evaluation, the complete human-readable evaluation, fix prompts, and metrics. Development runs write only to the development endpoint; live runs write only to the live endpoint. Four local artifacts are retained for evaluation, debugging, and persistence-failure recovery.

Until the Rentee AI Testing admin page exists, inspect results at **Bubble → Data → App data → BenchmarkRun**. Partial conversation failures are evaluated and persisted with `status=fail` where possible. Infrastructure failures use `status=error`; if Bubble persistence itself fails, local artifacts remain and the safe error appears in Render Logs and `/admin/benchmark_status`.

## Live benchmark safety

Development remains the default. A live run must explicitly request `environment: "live"` and Render must have `BENCHMARK_LIVE_ENABLED=true`.

The live benchmark uses a dedicated synthetic Lead where `Lead.test = true`. Before any live benchmark reset or mutation, the runner verifies:

- the separately saved live Lead exists;
- `Lead.test` is exactly `true`;
- the separately saved live Folio exists;
- `Folio.lead` points to that verified Lead.

If any verification fails, no live data is modified and the runner does not search for or fall back to another record. Reset only touches FolioItems referenced by the verified test Folio. Real customer Leads should have `test = false` or leave the field unset.

Development and live subject IDs are stored separately in `.autotest_state.json`, and live artifact filenames include `_live_` so histories remain separate.

After enabling live mode, trigger it with:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Benchmark-Key: <key>" \
  -d '{"environment":"live"}' \
  https://rentee-2.onrender.com/admin/run_benchmark
```

The endpoint does not accept Lead or Folio IDs. It only uses the dedicated IDs stored internally for the selected benchmark case.

## Human review to Codex handoff

`POST /admin/benchmark/<benchmark_run_id>/fix` incorporates the authoritative Bubble human review into `fixPrompt`, creates a Rentee task ID, records `working` in Bubble, and starts a background local Codex execution. `GET /admin/benchmark/<benchmark_run_id>/fix_status?environment=development|live` returns the Codex metadata currently stored on that BenchmarkRun. Both endpoints require `X-Benchmark-Key`.

The integration uses the officially documented non-interactive `codex exec` command. Render must have the Codex CLI and Git installed. Each task clones the public `chaz1505/rentee` repository from `main` into `/tmp/rentee-codex/<task-id>`, creates an isolated `codex/benchmark-...` branch, and runs Codex with the workspace-write sandbox. The deployed Render checkout is never modified.

Local Codex authentication uses the existing `OPENAI_API_KEY` inherited by the subprocess. No ChatGPT OAuth login, Codex Cloud environment, or Codex access token is required. The complete prompt is passed through stdin rather than command arguments. Configure optional `CODEX_EXEC_TIMEOUT_SECONDS` to override the 900-second execution limit.

The BenchmarkRun must expose these fields:

```text
codexSubmitted
codexSubmittedAt
codexStatus
codexTaskID
```

`working`, legacy `submitted`, and `completed` prevent duplicate execution; `failed` allows a retry. The HTTP endpoint returns `202` after starting a daemon background thread. Bubble changes to `completed` or `failed` when local execution finishes. `codexTaskID` is a Rentee-generated correlation ID, not an OpenAI cloud-task ID. Workspaces are retained for the later PR phase; workspaces older than 24 hours are removed when a new task starts.
