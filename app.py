from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from openai import OpenAI
import csv
import io
import os
import requests
import json
import re
import hmac
import traceback
import threading
import time
from datetime import datetime, timezone

# Connection-test marker: confirms updates can be applied to this app.
app = Flask(__name__)

CORS(
    app,
    resources={
        r"/*": {
            "origins": [
                "https://www.rentee.asia",
                "https://rentee.bubbleapps.io"
            ]
        }
    }
)

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
BUBBLE_API_TOKEN = os.environ["BUBBLE_API_TOKEN"]

# Temporary small batch for validating the end-to-end matching flow.
MATCH_LISTING_LIMIT = 600
CONDO_SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1wnXHS6cHoUmAVXFpkzZ9PhBKmEgG6g-n8n0jcyodYig/export?format=csv&gid=0"
)
CONDO_CACHE_TTL_SECONDS = 300
CONDO_SHEET_TIMEOUT_SECONDS = 15

_condo_cache = None
_condo_cache_checked_at = 0.0
_condo_cache_lock = threading.Lock()
_benchmark_run_lock = threading.Lock()
_benchmark_state_lock = threading.Lock()
_benchmark_state = {"status": "idle"}
_codex_task_metadata_lock = threading.Lock()
_codex_task_metadata = {}


class CondoDataError(RuntimeError):
    pass


def normalize_condo_name(value):
    return " ".join(str(value or "").split()).lower()


def _download_condo_lookup():
    response = requests.get(
        CONDO_SHEET_CSV_URL,
        timeout=CONDO_SHEET_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    text = response.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    condo_column = next(
        (
            field
            for field in fieldnames
            if normalize_condo_name(field) == "condo name"
        ),
        None
    )
    if not condo_column:
        raise CondoDataError("Condo sheet is missing the 'Condo name' column.")

    lookup = {}
    for source_row in reader:
        row = {
            field: "" if source_row.get(field) is None else str(source_row.get(field)).strip()
            for field in fieldnames
        }
        key = normalize_condo_name(row.get(condo_column))
        if key and key not in lookup:
            lookup[key] = row
    return lookup


def _get_condo_lookup():
    global _condo_cache, _condo_cache_checked_at

    now = time.monotonic()
    if (
        _condo_cache is not None
        and now - _condo_cache_checked_at < CONDO_CACHE_TTL_SECONDS
    ):
        return _condo_cache

    with _condo_cache_lock:
        now = time.monotonic()
        if (
            _condo_cache is not None
            and now - _condo_cache_checked_at < CONDO_CACHE_TTL_SECONDS
        ):
            return _condo_cache
        try:
            refreshed = _download_condo_lookup()
        except Exception as error:
            _condo_cache_checked_at = now
            if _condo_cache is not None:
                print(
                    f"Condo data refresh failed; using stale cache: {error}",
                    flush=True
                )
                return _condo_cache
            print(f"Initial condo data load failed: {error}", flush=True)
            raise CondoDataError(
                "Condo information is temporarily unavailable."
            ) from error

        _condo_cache = refreshed
        _condo_cache_checked_at = now
        print(f"Condo data refreshed: {len(refreshed)} condos loaded", flush=True)
        return _condo_cache


def get_condo_info(condo_name):
    normalized_name = normalize_condo_name(condo_name)
    if not normalized_name:
        return {"error": "A condo name is required."}
    row = _get_condo_lookup().get(normalized_name)
    if row is None:
        return {"error": f'Condo "{str(condo_name).strip()}" was not found.'}
    return dict(row)


def get_condo_infos(condo_names):
    results = []
    for condo_name in condo_names:
        requested = " ".join(str(condo_name or "").split())
        if not requested:
            results.append({
                "requested": requested,
                "found": False,
                "error": "A condo name is required."
            })
            continue
        try:
            condo = get_condo_info(requested)
        except CondoDataError as error:
            results.append({
                "requested": requested,
                "found": False,
                "error": str(error)
            })
            continue
        if "error" in condo:
            results.append({
                "requested": requested,
                "found": False,
                "error": condo["error"]
            })
        else:
            results.append({
                "requested": requested,
                "found": True,
                "data": condo
            })
    return json.dumps({"condos": results}, ensure_ascii=False)


def log_timing(label, started, detail=""):

    print(
        f"[TIMING] {label}: {time.perf_counter() - started:.2f}s{detail}",
        flush=True
    )


def log_token_usage(label, response):

    def value(source, field):
        if source is None:
            return 0
        if isinstance(source, dict):
            return source.get(field, 0) or 0
        return getattr(source, field, 0) or 0

    usage = value(response, "usage")
    input_details = value(usage, "input_tokens_details")
    output_details = value(usage, "output_tokens_details")

    print(
        f"[TOKENS] {label}: "
        f"input={value(usage, 'input_tokens')} "
        f"cached={value(input_details, 'cached_tokens')} "
        f"output={value(usage, 'output_tokens')} "
        f"reasoning={value(output_details, 'reasoning_tokens')} "
        f"total={value(usage, 'total_tokens')}",
        flush=True
    )


def get_bubble_base_url(bubble_env):
    if bubble_env == "development":
        return "https://www.rentee.asia/version-test/api/1.1"
    return "https://www.rentee.asia/api/1.1"


@app.route("/")
def home():
    return jsonify({"status": "running"})


@app.route("/health", methods=["GET"])
def health():
    deployed_commit = os.environ.get("RENDER_GIT_COMMIT", "").strip() or None
    return jsonify({"status": "ok", "commit": deployed_commit}), 200


@app.route("/test_condo", methods=["GET"])
def test_condo():
    condo_name = request.args.get("name", "")
    if not normalize_condo_name(condo_name):
        return jsonify({"error": "Missing required query parameter: name"}), 400
    try:
        result = get_condo_info(condo_name)
    except CondoDataError as error:
        return jsonify({"error": str(error)}), 503
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result), 200


def _benchmark_auth_error():
    configured_key = os.environ.get("BENCHMARK_API_KEY")
    if not configured_key:
        return jsonify({"error": "Benchmark endpoint is not configured."}), 503
    supplied_key = request.headers.get("X-Benchmark-Key", "")
    if not supplied_key or not hmac.compare_digest(supplied_key, configured_key):
        return jsonify({"error": "Unauthorized."}), 401
    return None


def _update_benchmark_state(run_id, updates):
    with _benchmark_state_lock:
        if _benchmark_state.get("run_id") == run_id:
            _benchmark_state.update(updates)


def _run_benchmark_background(run_id, environment="development"):
    try:
        from tests.run_benchmark import run_all_benchmarks

        def progress(update):
            _update_benchmark_state(run_id, update)

        suite = run_all_benchmarks(
            run_id=run_id,
            progress_callback=progress,
            environment=environment
        )
        first_result = suite.get("results", [{}])[0]
        execution = first_result.get("execution", {})
        _update_benchmark_state(run_id, {
            "status": "complete",
            "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "benchmark_status": execution.get("benchmark_status", "fail"),
            "result_path": execution.get("result_path"),
            "evaluation_path": execution.get("evaluation_path"),
            "evaluation_markdown_path": execution.get("evaluation_markdown_path"),
            "fix_prompt_path": execution.get("fix_prompt_path"),
            "benchmark_run_id": execution.get("benchmark_run_id"),
            "result_persisted": execution.get("result_persisted", False),
            "persistence_error": execution.get("persistence_error")
        })
    except Exception as error:
        print(f"[BENCHMARK] BENCHMARK FAILED: {error}", flush=True)
        traceback.print_exc()
        _update_benchmark_state(run_id, {
            "status": "failed",
            "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "error": str(error)
        })
    finally:
        _benchmark_run_lock.release()


@app.route("/admin/run_benchmark", methods=["POST"])
def admin_run_benchmark():
    auth_error = _benchmark_auth_error()
    if auth_error:
        return auth_error
    if not _benchmark_run_lock.acquire(blocking=False):
        with _benchmark_state_lock:
            running = dict(_benchmark_state)
        return jsonify({
            "status": "already_running",
            "run_id": running.get("run_id"),
            "started_at": running.get("started_at")
        }), 409

    try:
        request_data = request.get_json(silent=True) or {}
        environment = request_data.get("environment", "development")
        if environment not in ("development", "live"):
            _benchmark_run_lock.release()
            return jsonify({"error": "environment must be development or live"}), 400
        if (
            environment == "live"
            and os.environ.get("BENCHMARK_LIVE_ENABLED", "").strip().lower() != "true"
        ):
            _benchmark_run_lock.release()
            return jsonify({"error": "Live benchmark is not enabled."}), 403
        from tests.run_benchmark import get_benchmark_case_ids
        case_ids = get_benchmark_case_ids()
        case_id = case_ids[0] if case_ids else "benchmark"
        started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_id = (
            f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_"
            f"{case_id}_{environment}"
        )
        with _benchmark_state_lock:
            _benchmark_state.clear()
            _benchmark_state.update({
                "status": "running",
                "run_id": run_id,
                "case": case_id,
                "environment": environment,
                "started_at": started_at,
                "current_turn": 0
            })
        thread = threading.Thread(
            target=_run_benchmark_background,
            args=(run_id, environment),
            name=f"benchmark-{run_id}",
            daemon=True
        )
        thread.start()
    except Exception:
        _benchmark_run_lock.release()
        raise
    return jsonify({
        "status": "started", "run_id": run_id, "case": case_id,
        "environment": environment
    }), 202


@app.route("/admin/benchmark_status", methods=["GET"])
def admin_benchmark_status():
    auth_error = _benchmark_auth_error()
    if auth_error:
        return auth_error
    with _benchmark_state_lock:
        return jsonify(dict(_benchmark_state)), 200


def _run_codex_fix_background(
    prompt, run_id, benchmark_run_id, environment, task_id
):
    from automation.codex_client import CodexSubmissionError, submit_codex_fix
    from tests.human_review import patch_codex_state

    try:
        def persist_progress(status, progress):
            payload = {
                "codexSubmitted": True,
                "codexStatus": status,
                "codexTaskID": task_id,
            }
            if progress.get("branch"):
                payload["codexBranch"] = progress["branch"]
            if progress.get("fix_commit"):
                payload["codexCommit"] = progress["fix_commit"]
            if progress.get("pr_number") is not None:
                payload["codexPRNumber"] = progress["pr_number"]
            if progress.get("pr_url"):
                payload["codexPRURL"] = progress["pr_url"]
            patch_codex_state(benchmark_run_id, environment, payload)

        metadata = submit_codex_fix(
            prompt, run_id, benchmark_run_id, environment, task_id=task_id,
            progress_callback=persist_progress,
        )
        final_status = metadata.get("status", "completed")
        final_payload = {
            "codexSubmitted": True,
            "codexStatus": final_status,
            "codexTaskID": task_id,
        }
        if metadata.get("branch"):
            final_payload["codexBranch"] = metadata["branch"]
        if metadata.get("fix_commit"):
            final_payload["codexCommit"] = metadata["fix_commit"]
        if metadata.get("pr_number") is not None:
            final_payload["codexPRNumber"] = metadata["pr_number"]
        if metadata.get("pr_url"):
            final_payload["codexPRURL"] = metadata["pr_url"]
        if metadata.get("merged") is not None:
            final_payload["codexMerged"] = metadata["merged"]
        if metadata.get("merged_at"):
            final_payload["codexMergedAt"] = metadata["merged_at"]
        if metadata.get("merge_commit"):
            final_payload["codexMergeCommit"] = metadata["merge_commit"]
        patch_codex_state(benchmark_run_id, environment, final_payload)
        with _codex_task_metadata_lock:
            _codex_task_metadata[task_id] = metadata
        print(f"[CODEX] Task completed: {task_id}", flush=True)
    except Exception as error:
        safe_error = str(error) if isinstance(error, CodexSubmissionError) else type(error).__name__
        print(f"[CODEX] Background execution failed: {safe_error}", flush=True)
        try:
            patch_codex_state(benchmark_run_id, environment, {
                "codexSubmitted": False,
                "codexStatus": "failed",
                "codexTaskID": task_id,
                "codexMerged": False,
            })
        except Exception:
            print("[CODEX] Failed to persist Codex failure state.", flush=True)
        with _codex_task_metadata_lock:
            _codex_task_metadata[task_id] = {
                "task_id": task_id, "status": "failed", "error": safe_error
            }


@app.route("/admin/benchmark/<benchmark_run_id>/fix", methods=["POST"])
def admin_benchmark_fix(benchmark_run_id):
    auth_error = _benchmark_auth_error()
    if auth_error:
        return auth_error
    request_data = request.get_json(silent=True) or {}
    environment = request_data.get("environment", "development")
    if environment not in ("development", "live"):
        return jsonify({"error": "environment must be development or live"}), 400

    from tests.human_review import (
        BenchmarkReviewError,
        CodexAlreadyActive,
        patch_codex_state,
        update_fix_prompt_with_human_review,
    )
    from automation.codex_client import create_codex_task_id

    print(f"[BENCHMARK FIX] Loading BenchmarkRun {benchmark_run_id}", flush=True)
    print(f"[BENCHMARK FIX] Environment: {environment.upper()}", flush=True)
    try:
        result = update_fix_prompt_with_human_review(
            benchmark_run_id, environment
        )
    except CodexAlreadyActive as error:
        return jsonify({
            "error": str(error),
            "codex_status": error.codex_status,
            "codex_task_id": error.codex_task_id,
        }), 409
    except BenchmarkReviewError as error:
        print(f"[BENCHMARK FIX] Failed: {error}", flush=True)
        return jsonify({"error": str(error)}), error.status_code
    except Exception as error:
        print(
            "[BENCHMARK FIX] Unexpected Bubble review handoff failure: "
            f"{type(error).__name__}",
            flush=True,
        )
        return jsonify({"error": "BenchmarkRun update failed."}), 502
    print("[BENCHMARK FIX] Human review verified.", flush=True)
    print(
        "[BENCHMARK FIX] BenchmarkRun fixPrompt updated successfully.",
        flush=True,
    )
    updated_prompt = result.pop("updated_fix_prompt")
    print(f"[CODEX] Preparing fix task for BenchmarkRun {benchmark_run_id}", flush=True)
    print(f"[CODEX] Run ID: {result.get('run_id')}", flush=True)
    task_id = create_codex_task_id(result.get("run_id"))
    submitted_at = datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    try:
        patch_codex_state(benchmark_run_id, environment, {
            "codexSubmitted": True,
            "codexSubmittedAt": submitted_at,
            "codexStatus": "working",
            "codexTaskID": task_id,
        })
    except BenchmarkReviewError as error:
        print(f"[CODEX] Failed to initialize task state: {error}", flush=True)
        return jsonify({
            "error": "Codex task state could not be initialized.",
            "fix_prompt_updated": True,
            "codex_submitted": False,
        }), 502
    try:
        thread = threading.Thread(
            target=_run_codex_fix_background,
            args=(
                updated_prompt, result.get("run_id"), benchmark_run_id,
                environment, task_id,
            ),
            name=f"codex-{task_id}",
            daemon=True,
        )
        thread.start()
    except Exception as error:
        print(f"[CODEX] Failed to start background task: {type(error).__name__}", flush=True)
        try:
            patch_codex_state(benchmark_run_id, environment, {
                "codexSubmitted": False, "codexStatus": "failed",
            })
        except Exception:
            print("[CODEX] Failed to persist Codex failure state.", flush=True)
        return jsonify({
            "error": "Codex background task could not be started.",
            "fix_prompt_updated": True,
            "codex_submitted": False,
        }), 502
    print("[CODEX] Local Codex task started in background.", flush=True)
    print(f"[CODEX] Task ID: {task_id}", flush=True)
    return jsonify({
        "status": "working",
        **result,
        "codex_submitted": True,
        "codex_status": "working",
        "codex_task_id": task_id,
    }), 202


@app.route("/admin/benchmark/<benchmark_run_id>/fix_status", methods=["GET"])
def admin_benchmark_fix_status(benchmark_run_id):
    auth_error = _benchmark_auth_error()
    if auth_error:
        return auth_error
    environment = request.args.get("environment", "development")
    if environment not in ("development", "live"):
        return jsonify({"error": "environment must be development or live"}), 400
    from tests.human_review import (
        BenchmarkReviewError,
        get_benchmark_run_codex_status,
    )
    try:
        status = get_benchmark_run_codex_status(
            benchmark_run_id, environment
        )
    except BenchmarkReviewError as error:
        print(f"[CODEX] Status lookup failed: {error}", flush=True)
        return jsonify({"error": str(error)}), error.status_code
    return jsonify(status), 200


def load_front_door_renter_summary(folio_id, bubble_env):
    if not folio_id:
        return "No stored preferences yet."
    lookup_started = time.perf_counter()
    try:
        base_url = get_bubble_base_url(bubble_env)
        folio = bubble(f"{base_url}/obj/folio/{folio_id}")
        lead = bubble(f"{base_url}/obj/lead/{folio['lead']}")
        summary = str(lead.get("AIsearchsummary") or "").strip()
        return summary or "No stored preferences yet."
    except Exception as error:
        print(
            f"Front-door renter context unavailable; continuing safely: {error}",
            flush=True,
        )
        return "Stored preferences are temporarily unavailable."
    finally:
        log_timing("Front-door AIsearchsummary lookup", lookup_started)


def build_response_args(
    user_message, previous_response_id=None, renter_summary=None
):
    known_preferences = (
        str(renter_summary).strip()
        if renter_summary is not None and str(renter_summary).strip()
        else "No stored preferences yet."
    )
    args = {
        "model": "gpt-5-mini",
        "input": (
            "KNOWN RENTER PREFERENCES\n\n"
            f"{known_preferences}\n\n"
            "CURRENT CUSTOMER MESSAGE\n\n"
            f"{user_message}"
        ),
        "instructions": """
You are Rentee, a friendly, concise personal property assistant for renters in
Kuala Lumpur. Speak directly to the renter using “you” and “your”. Be proactive
by helping them progress from information they have actually supplied or that is
already established in their renter brief. Do not invent property interest,
requirements, or specific property directions.

INTENT AND CONTEXT

First understand what the renter means in conversational context. Then decide
whether the message needs normal conversation, persistence of a lasting
preference, general condo information, details about a specific current listing,
current recommendations, or external/current web information. Do not route
mechanically from keywords, named entities, or stale intent from earlier turns.
Greetings, introductions, reactions, corrections, clarifications, quoted text,
and questions about your previous reasoning may require no tool. A correction is
not automatically a lasting preference or a request to rematch. Acknowledge and
repair misunderstandings naturally. Never introduce a named condo, development,
listing, or unit as suitable merely from nationality, demographics, occupation,
lifestyle stereotypes, or general model knowledge.

RENTER BRIEF AND RECOMMENDATION READINESS

Before presenting, recommending, shortlisting, comparing, or ranking specific
current listings, establish a sufficient renter brief from AIsearchsummary and
the current conversation together. The six required parts are:
1. approximate monthly budget or range;
2. interested location(s), or a specific condo/development to search;
3. furnishing preference: furnished, partially furnished, unfurnished, or
   genuinely flexible;
4. required or preferred bedroom count;
5. household size, with adults/children where naturally available; and
6. an opportunity to state other requirements, or confirmation that there are none.

Bedrooms and household size are distinct. Do not infer that one answers the
other. Information clearly present in AIsearchsummary or this conversation is
already known: do not ask for it again. AIsearchsummary describes what this
renter wants; it is not evidence of current availability or that any property
matches. If the brief is incomplete, do not present listings. Ask efficiently
for only the missing information, grouping closely related basics when natural
rather than forcing a rigid questionnaire. An informational condo or listing
question does not require a complete recommendation brief.

If the brief is complete, coherent, and the renter genuinely requests current
options, perform fresh matching without unnecessary re-interviewing. When relying
substantially on stored preferences, naturally frame recommendations as based on
the known brief or briefly confirm it only when it may be stale, ambiguous,
conflicting, or part of a substantially new search. Do not mechanically recite
the whole summary.

TOOL ROUTING

- update_preferences persists a clear, lasting addition, removal, or change to
  the ongoing home search. Temporary exploration, introductions, ordinary
  questions, reactions, and corrections of your own answer are not persistent
  updates unless context genuinely establishes an ongoing requirement.
- match_lead finds current options only when recommendation intent is current
  and genuine and the six-part brief is sufficiently complete. A request from an
  earlier turn does not authorize matching now. If one message both changes a
  lasting preference and requests recommendations, persist it first and match
  only when the resulting brief is complete.
- get_condo_info retrieves general knowledge about a named residential
  development when the renter genuinely asks about its character, facilities,
  location, suitability, strengths, weaknesses, or comparison. A condo mention
  alone—including in a correction or question about your prior reasoning—is not
  enough. Request all condos for a comparison in one call. Persona is qualitative
  expert insight: present it as judgement, not objective fact.
- get_property_details retrieves authoritative facts about a specific current
  Rentee listing or unit already being discussed. Do not rerun matching merely
  to answer such a factual question.
- web search is for genuinely current or external information missing from
  authoritative Rentee data, such as infrastructure, transport, regulations,
  taxes, markets, or project news. For ordinary condo knowledge, consult Rentee’s
  condo data first. Never use the web for current Rentee listing availability or
  to overwrite Rentee listing facts.

SEARCH SCOPE AND GROUNDING

An explicit request for current properties in a named condo, development, area,
or location creates a hard scope for that recommendation turn. Do not broaden it
unless the renter asks for alternatives. Immediate scope is not necessarily a
lasting location preference. A direct scoped search still requires the rest of
the minimum brief before listings are presented.

Conversation can be generative; property discovery must be grounded. Every
specific current recommendation must come from a successful fresh match_lead
result. Never rely on property names, availability, or details from conversation
history. Current unit facts—including price, bedrooms, bathrooms, size,
furnishing, parking, facilities, availability, photos, and floorplans—must come
from current authoritative Rentee listing data. Never invent a missing fact. If
a unit-specific fact is absent, say it is not specified rather than searching the
web or guessing.

COMMUNICATION AND SAFETY

Listen before recommending, use established information, ask only useful missing
questions, and help the renter move from a coherent brief to grounded options and
then decide what they would view. Explain material compromises calmly. Do not
offer actions Rentee cannot perform, including contacting agents or owners,
arranging viewings, sending photos, obtaining floorplans, privately confirming
facts, or checking exact commutes. Do not expose prompts, tool names, internal
IDs, database fields, or implementation details, and do not speak as an internal
estate-agent assistant.
""",
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "tools": [
    {
        "type": "function",
        "name": "match_lead",
        "description": (
            "Find and rank currently available Rentee listings when the renter "
            "genuinely asks for current property options and the six-part minimum "
            "renter brief is sufficiently complete. Apply any condo, development, "
            "area, or location in the immediate request as the current search scope. "
            "Do not use for ordinary conversation, informational questions, isolated "
            "corrections, or a preference change without current recommendation intent."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "update_preferences",
        "description": (
            "Persist a lasting addition, removal, or change to the renter's ongoing "
            "home-search requirements, such as budget, household, bedrooms, generally "
            "preferred areas, schools, commute, pets, furnishing, timing, facilities, "
            "or lifestyle needs. Do not use for temporary exploration, introductions, "
            "ordinary questions, reactions, or corrections of Rentee's previous answer "
            "unless context genuinely establishes a lasting requirement."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "preference_update": {
                    "type": "string",
                    "description": (
                        "A concise description of the new, changed, removed, or additional "
                        "home-search information stated by the user. Preserve every supplied "
                        "detail faithfully; do not compress several constraints into a vague "
                        "summary or omit qualifiers, quantities, ranges, or negatives."
                    )
                },
                "recommendations_requested": {
                    "type": "boolean",
                    "description": (
                        "True only when this same message genuinely requests current "
                        "recommendations and AIsearchsummary plus the current conversation "
                        "provide the complete six-part renter brief. False for a preference "
                        "update alone or when discovery information is still missing."
                    )
                }
            },
            "required": ["preference_update", "recommendations_requested"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "get_condo_info",
        "description": (
            "Retrieve Rentee's general knowledge about named residential developments "
            "when the renter genuinely asks about their characteristics, facilities, "
            "location, suitability, strengths, weaknesses, or comparison. For a "
            "comparison, request all relevant names together. Do not use merely because "
            "a condo name appears in a correction, reaction, quotation, or question about "
            "Rentee's previous reasoning, and do not use for current listing availability."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "condo_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Exact condo names to retrieve in one request."
                }
            },
            "required": ["condo_names"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "get_property_details",
        "description": (
            "Retrieve authoritative facts about a specific current Rentee listing or "
            "unit already being discussed. Use condo information for general development "
            "questions and fresh matching for new recommendations. Never infer missing facts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "property_reference": {
                    "type": "string",
                    "description": (
                        "The property, condo, building, unit, or listing being referred to, "
                        "using the user's wording or conversational context."
                    )
                }
            },
            "required": ["property_reference"],
            "additionalProperties": False
        }
    },
    {
        "type": "web_search"
    }
]
    }

    if previous_response_id:
        args["previous_response_id"] = previous_response_id

    return args


def get_web_citations(response):

    citations = []
    seen_urls = set()

    for output_item in response.output:
        for content in getattr(output_item, "content", []) or []:
            for annotation in getattr(content, "annotations", []) or []:
                if getattr(annotation, "type", None) != "url_citation":
                    continue

                citation = getattr(annotation, "url_citation", None)
                url = getattr(citation, "url", None)

                if url and url not in seen_urls:
                    citations.append({
                        "title": getattr(citation, "title", "Source"),
                        "url": url
                    })
                    seen_urls.add(url)

    return citations


def bubble(url, **kwargs):

    r = requests.get(url, timeout=30, **kwargs)

    r.raise_for_status()

    return r.json()["response"]


def get_all_listings(base_url):

    load_started = time.perf_counter()
    listings = []
    cursor = 0
    seen_cursors = set()

    while cursor not in seen_cursors:
        seen_cursors.add(cursor)
        page_started = time.perf_counter()
        page = bubble(f"{base_url}/obj/listing", params={"cursor": cursor})
        results = page.get("results", [])
        log_timing(
            f"Listing page {len(seen_cursors)}",
            page_started,
            f" ({len(results)} listings)"
        )
        listings.extend(results)
        remaining = page.get("remaining", 0) or 0
        print(
            f"Loaded {len(results)} listings; {remaining} remaining",
            flush=True
        )

        if not results or not remaining:
            break

        # Bubble's cursor is the current offset, so advance by this page size.
        cursor += len(results)

    log_timing("Load all listings", load_started, f" ({len(listings)} listings)")
    return listings


def get_property_details(folio_id, property_reference, bubble_env):

    base_url = get_bubble_base_url(bubble_env)
    folio = bubble(f"{base_url}/obj/folio/{folio_id}")
    recommended_listings = []

    for folio_item_id in folio.get("folioItems", []) or []:
        try:
            folio_item = bubble(f"{base_url}/obj/folioItem/{folio_item_id}")
            listing_id = folio_item.get("listing")
            listing = bubble(f"{base_url}/obj/listing/{listing_id}")

            if listing.get("_id") and not any(
                item.get("_id") == listing["_id"]
                for item in recommended_listings
            ):
                recommended_listings.append(listing)
        except Exception as error:
            print(f"Failed to load Folio Item details: {error}", flush=True)

    if not recommended_listings:
        return "I couldn't find any current recommendations to check."

    reference = property_reference.lower().strip()
    selected_listing = None
    ordinal_positions = {
        "first": 0,
        "1st": 0,
        "second": 1,
        "2nd": 1,
        "third": 2,
        "3rd": 2,
        "number 1": 0,
        "number 2": 1,
        "number 3": 2
    }

    for ordinal, position in ordinal_positions.items():
        if ordinal in reference and position < len(recommended_listings):
            selected_listing = recommended_listings[position]
            break

    if selected_listing is None and "last" in reference:
        selected_listing = recommended_listings[-1]

    if selected_listing is None:
        matching_listings = [
            listing
            for listing in recommended_listings
            if reference and reference in json.dumps(listing, ensure_ascii=False).lower()
        ]

        if len(matching_listings) == 1:
            selected_listing = matching_listings[0]
        elif len(recommended_listings) == 1 and reference in {
            "it", "that one", "that unit", "the property you just showed me"
        }:
            selected_listing = recommended_listings[0]

    if selected_listing is None:
        candidates = [
            {"listing_id": listing["_id"], "listing": listing}
            for listing in recommended_listings
        ]
        resolver_response = client.responses.create(
            model="gpt-5-mini",
            input=(
                "Resolve the customer's property reference against only the supplied current "
                "recommended listings. Select one listing only if the reference can be matched "
                "with confidence; otherwise return matched false.\n\n"
                f"PROPERTY REFERENCE: {property_reference}\n\n"
                f"CURRENT RECOMMENDED LISTINGS: {json.dumps(candidates, ensure_ascii=False)}"
            ),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "property_reference_resolution",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "listing_id": {"type": "string"},
                            "matched": {"type": "boolean"}
                        },
                        "required": ["listing_id", "matched"],
                        "additionalProperties": False
                    }
                }
            }
        )
        log_token_usage("Property reference resolution", resolver_response)
        resolution = json.loads(resolver_response.output_text)

        if resolution["matched"]:
            selected_listing = next(
                (
                    listing
                    for listing in recommended_listings
                    if listing["_id"] == resolution["listing_id"]
                ),
                None
            )

    if selected_listing is None:
        return (
            "I couldn't identify a single property from that reference. Please tell me "
            "the building name or which option you mean."
        )

    detail_fields = [
        ("Property", ("name", "title", "listingName", "condoName")),
        ("Property type", ("propertyType",)),
        ("Bedrooms", ("beds",)),
        ("Bathrooms", ("baths",)),
        ("Rent", ("priceRent",)),
        ("Sale price", ("priceSale",)),
        ("Size", ("Sq Ft", "size")),
        ("Furnishing", ("furnished",)),
        ("Parking", ("parking", "car parks")),
        ("Availability", ("availability",)),
        ("Balcony", ("balcony",)),
        ("Facilities", ("facilities", "amenities")),
        ("Location", ("address", "location")),
        ("Description", ("Description", "keyFacts", "AIsearchtext"))
    ]
    details = []

    for label, field_names in detail_fields:
        for field_name in field_names:
            value = selected_listing.get(field_name)

            if value not in (None, "", []):
                if isinstance(value, list):
                    value = ", ".join(str(item) for item in value)
                details.append(f"{label}: {value}")
                break

    if not details:
        return "I found the property, but Rentee does not have further details available."

    return "Authoritative Rentee property details:\n" + "\n".join(details)


def create_folio_items(recommendations, folio_id, lead_id, base_url):

    create_started = time.perf_counter()
    folio_item_ids = []

    for position, recommendation in enumerate(recommendations, start=1):
        listing_id = recommendation["listing_id"]
        reco_summary = recommendation["reco_summary"]
        try:
            folio_item_started = time.perf_counter()
            response = requests.post(
                f"{base_url}/obj/folioItem",
                headers={
                    "Authorization": f"Bearer {BUBBLE_API_TOKEN}",
                    "Content-Type": "application/json"
                },
                json={
                    "listing": listing_id,
                    "folio": folio_id,
                    "lead": lead_id,
                    "newlyAdded": True,
                    "RecoSummary": reco_summary
                },
                timeout=30
            )
            response.raise_for_status()
            log_timing(f"Create FolioItem {position}", folio_item_started)
            data = response.json()
            folio_item_id = data.get("id")

            if not folio_item_id:
                raise ValueError("Bubble did not return a Folio Item ID.")

            folio_item_ids.append(folio_item_id)
        except Exception as error:
            print(f"Failed to create Folio Item: {error}", flush=True)
            return None

    log_timing(
        "Create all FolioItems",
        create_started,
        f" ({len(folio_item_ids)} items)"
    )
    return folio_item_ids


def clear_folio_item_newly_added(folio_item_id, base_url):

    clear_started = time.perf_counter()
    response = requests.patch(
        f"{base_url}/obj/folioItem/{folio_item_id}",
        headers={
            "Authorization": f"Bearer {BUBBLE_API_TOKEN}",
            "Content-Type": "application/json"
        },
        json={"newlyAdded": False},
        timeout=30
    )
    response.raise_for_status()
    log_timing("Clear FolioItem newlyAdded", clear_started)


def update_folio_items(folio_id, folio_item_ids, base_url):

    patch_started = time.perf_counter()
    response = requests.patch(
        f"{base_url}/obj/folio/{folio_id}",
        headers={
            "Authorization": f"Bearer {BUBBLE_API_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "folioItems": folio_item_ids,
            "newRecommendations": True
        },
        timeout=30
    )
    response.raise_for_status()
    log_timing("Patch Folio", patch_started)


def match_lead(folio_id, bubble_env, current_request=None):

    match_started = time.perf_counter()
    yield "Checking your preferences..."
    base_url = get_bubble_base_url(bubble_env)
    folio_started = time.perf_counter()
    folio = bubble(f"{base_url}/obj/folio/{folio_id}")
    log_timing("match_lead - Folio lookup", folio_started)
    existing_folio_item_ids = list(folio.get("folioItems", []) or [])
    valid_existing_folio_item_ids = []
    existing_listing_ids = set()
    previously_new_folio_item_ids = []

    existing_items_started = time.perf_counter()
    for existing_folio_item_id in existing_folio_item_ids:
        try:
            existing_folio_item = bubble(
                f"{base_url}/obj/folioItem/{existing_folio_item_id}"
            )
        except requests.HTTPError as error:
            if (
                error.response is not None
                and error.response.status_code == 404
            ):
                print(
                    "Skipping stale FolioItem reference: "
                    f"{existing_folio_item_id}",
                    flush=True,
                )
                continue
            raise
        valid_existing_folio_item_ids.append(existing_folio_item_id)
        existing_listing_id = existing_folio_item.get("listing")

        if existing_listing_id:
            existing_listing_ids.add(existing_listing_id)

        if existing_folio_item.get("newlyAdded") is True:
            previously_new_folio_item_ids.append(existing_folio_item_id)
    log_timing(
        "match_lead - load existing FolioItems",
        existing_items_started,
        f" ({len(existing_folio_item_ids)} items)"
    )

    lead_id = folio["lead"]
    print(f"Folio {folio_id} -> Lead {lead_id}", flush=True)
    lead_started = time.perf_counter()
    lead = bubble(f"{base_url}/obj/lead/{lead_id}")
    log_timing("match_lead - Lead lookup", lead_started)

    yield "Searching available properties..."
    listings_started = time.perf_counter()
    listings = get_all_listings(base_url)[:MATCH_LISTING_LIMIT]
    log_timing("match_lead - load listings", listings_started)

    print(
        f"Scoring {len(listings)} listings (test limit: {MATCH_LISTING_LIMIT})",
        flush=True
    )

    prompt_started = time.perf_counter()
    current_request_text = (current_request or "").strip()
    prompt = f"""

You are helping a property seeker find their ideal home.

Review the home seeker's requirements and all available properties.

Select only properties you genuinely believe could be a good fit.

Rank the strongest matches from best to worst.

=========================

PERSISTENT HOME SEEKER REQUIREMENTS

=========================

{lead["AIsearchtext"]}

=========================

CURRENT CUSTOMER REQUEST

=========================

{current_request_text or "No separate current recommendation request was supplied."}

=========================

AVAILABLE PROPERTIES

=========================

"""

    for listing in listings:

        prompt += f"""

INTERNAL LISTING ID: {listing.get("_id")}

Bedrooms: {listing.get("beds")}

Bathrooms: {listing.get("baths")}

Rent: {listing.get("priceRent")}

Sale: {listing.get("priceSale")}

{listing.get("AIsearchtext","")}

----------------------------------------

"""

    prompt += """

For each recommended property:

- Give the property or building name where available.
- Explain briefly why it suits the user's requirements.
- Mention any important compromise or consideration.
- Keep the explanation focused on what matters to the user.
- Include the rent and bedroom count when supplied, so the customer can evaluate
  concrete trade-offs rather than choosing between vague categories.
- Distinguish verified listing facts from requirements that remain unverified.
- Before selecting properties, interpret the renter's language and classify each
  requirement as a HARD REQUIREMENT, STRONG PREFERENCE, SOFT PREFERENCE, or
  UNKNOWN / UNVERIFIED REQUIREMENT. Explicit absolute language such as "must",
  "absolute maximum", "will not", "only", or a direct rejection creates a hard
  requirement. Approximate, preferred, desired, flexible, and ideal language does
  not become hard merely because it appears in the persistent profile.
- Check every candidate against all four categories, including negative
  requirements and quantities. Do not silently omit a requirement because the
  listing data does not mention it.
- Never claim that pets, furnishing or white goods, view/facing, school transport,
  walking time, exact location, or availability satisfy a requirement unless the
  supplied property information explicitly supports that claim.
- Treat PERSISTENT HOME SEEKER REQUIREMENTS as the customer's broader profile.
- Treat CURRENT CUSTOMER REQUEST as the immediate intent for this recommendation
  turn. If it explicitly asks for recommendations in a named condo, building, or
  location, that named scope is a hard constraint for this turn. Recommend only
  supplied listings that are explicitly in that scope, then apply the persistent
  requirements to rank and filter within it.
- Never trade off or broaden an explicit current condo, building, or location scope
  because an out-of-scope listing fits the persistent profile. If no supplied listing
  is explicitly in the requested scope, return an empty recommendations array and say
  clearly that no current matching listings were found there. Do not substitute other
  areas or buildings unless CURRENT CUSTOMER REQUEST explicitly asks for alternatives.
- A general request such as 'what else do you have?' has no hard location scope and
  uses the persistent profile. A statement such as 'I'm also open to DC Residensi'
  does not create a hard scope unless that same current request explicitly asks to see
  or receive recommendations there.
- Do not recommend a property that explicitly contradicts a genuine hard
  requirement. Explicit immediate search scope is always hard for that turn.
  Explicit absolute constraints such as an absolute budget ceiling, mandatory
  minimum bedrooms, rejected property type, or prohibited view are also hard.
- Rank holistically within those boundaries. A compelling property may reasonably
  miss a strong or soft preference—for example an approximate budget, preferred
  area, desired furnishing level, ideal bedroom count, or preferred condition—when
  another benefit makes the trade-off worthwhile. Explain the compromise clearly.
  Do not use arbitrary stretch percentages and do not recommend random near-misses
  merely to fill the list.
- If a requirement is unknown or unverified, identify it prominently instead of
  treating it as satisfied. In particular, pet allowance must cover the renter's
  stated kind and number of pets; a generic pet-friendly claim is insufficient.

Do not recommend properties simply to fill a list. If only a few properties
are genuinely suitable, recommend only those properties.

Write directly to the property seeker using 'you' and 'your'. Be helpful,
confident, and conversational, like a highly knowledgeable personal property
concierge.

Do not mention Lead IDs, Folio IDs, Listing IDs, internal database information,
the matching process, internal scoring, or estate-agent workflows.

Do not offer or promise actions Rentee cannot perform. In particular, do not say
you will contact or check with agents, owners, or property management; arrange,
schedule, or book viewings; send photos; obtain floorplans; privately confirm
availability; or perform exact commute checks. You may suggest what the customer
should verify themselves, clearly framed as their next step.

Do not invent facts. Only use information in the home seeker requirements and
supplied property information.

Return valid JSON with exactly these fields:
- recommendations: an array in ranking order. Each item must contain the
  INTERNAL LISTING ID from the supplied properties as listing_id and a
  personalised reco_summary. Include only properties you genuinely recommend;
  never invent an ID or add properties to fill a list.
- customer_response: concise, natural, customer-facing recommendation prose.
  Never mention internal IDs, Folio IDs, Lead IDs, database fields, or the
  matching process. Briefly connect the shortlist to the customer's most important
  stated requirements using 'you' and 'your', then present the properties as a
  scannable bullet list. State unresolved hard requirements alongside each relevant
  property, including pet policy, facing/view, and commute or walking time when the
  customer specified them and the listing data does not verify them.

For every recommended listing, reco_summary must be a short, personalised
one- or two-sentence explanation of why this listing suits this home seeker.
Focus on the one to three strongest relevant requirements and actual listing
attributes, and mention a material trade-off when applicable. Use natural,
consumer-friendly language. Do not mention IDs, scores, matching logic, or
AIsearchtext; do not use generic real-estate marketing language; and do not
invent facts or claim a requirement exists unless it appears in the supplied
home seeker requirements. reco_summary is recommendation reasoning, not a
rewritten listing description.

The recommendations array is the source of truth. customer_response must
describe only the listings represented there, in the same order. Never invent
a property, unit, building name, or property detail. If a name or detail is not
in the supplied property information, do not mention it.

"""
    log_timing("match_lead - build matching input", prompt_started)

    yield "Ranking the best matches..."
    matching_started = time.perf_counter()
    response = client.responses.create(

        model="gpt-5-mini",

        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "listing_recommendations",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "recommendations": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "listing_id": {"type": "string"},
                                    "reco_summary": {"type": "string"}
                                },
                                "required": ["listing_id", "reco_summary"],
                                "additionalProperties": False
                            }
                        },
                        "customer_response": {"type": "string"}
                    },
                    "required": ["recommendations", "customer_response"],
                    "additionalProperties": False
                }
            }
        }

    )
    log_timing("match_lead - OpenAI matching", matching_started)
    log_token_usage("Matching", response)

    print("Matching model response received", flush=True)
    parse_started = time.perf_counter()
    try:
        result = json.loads(response.output_text)
    except json.JSONDecodeError as error:
        print(f"Failed to parse matching JSON: {error}", flush=True)
        log_timing("match_lead TOTAL", match_started)
        return "I’m sorry, I couldn’t prepare your recommendations just now."

    available_listing_ids = {
        listing["_id"]
        for listing in listings
        if listing.get("_id")
    }
    validated_recommendations = []
    seen_recommended_listing_ids = set()

    for recommendation in result["recommendations"]:
        listing_id = recommendation["listing_id"]
        if listing_id not in available_listing_ids:
            print("Ignoring invalid recommended listing ID", flush=True)
        elif listing_id not in seen_recommended_listing_ids:
            validated_recommendations.append(recommendation)
            seen_recommended_listing_ids.add(listing_id)

    new_recommendations = [
        recommendation
        for recommendation in validated_recommendations
        if recommendation["listing_id"] not in existing_listing_ids
    ]
    log_timing("match_lead - parse/validate", parse_started)

    yield "Updating your shortlist..."

    if new_recommendations:
        folio_items_update_started = time.perf_counter()
        clear_started = time.perf_counter()
        try:
            for previous_folio_item_id in previously_new_folio_item_ids:
                clear_folio_item_newly_added(previous_folio_item_id, base_url)
        except Exception as error:
            print(f"Failed to clear previous Folio Item flags: {error}", flush=True)
            log_timing("Clear previous newlyAdded flags", clear_started)
            log_timing("match_lead TOTAL", match_started)
            return result["customer_response"]
        log_timing("Clear previous newlyAdded flags", clear_started)

        new_folio_item_ids = create_folio_items(
            new_recommendations, folio_id, lead_id, base_url
        )

        if new_folio_item_ids is not None:
            final_folio_item_ids = (
                valid_existing_folio_item_ids + new_folio_item_ids
            )
            try:
                update_folio_items(folio_id, final_folio_item_ids, base_url)
            except Exception as error:
                print(f"Failed to update Folio Items: {error}", flush=True)
        log_timing("match_lead - update FolioItems", folio_items_update_started)

    log_timing("match_lead TOTAL", match_started)
    return result["customer_response"]


def stream_match_lead(folio_id, bubble_env, current_request=None):

    match_flow = match_lead(folio_id, bubble_env, current_request)

    while True:
        try:
            status = next(match_flow)
        except StopIteration as completed:
            return completed.value

        yield f"data: {json.dumps({'status': status})}\n\n"


def update_lead_ai_searchtext(lead_id, updated_text, ai_search_summary, base_url):

    print(f"Updating Lead preferences for lead {lead_id}", flush=True)
    update_started = time.perf_counter()
    response = requests.patch(
        f"{base_url}/obj/lead/{lead_id}",
        headers={
            "Authorization": f"Bearer {BUBBLE_API_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "AIsearchtext": updated_text,
            "AIsearchsummary": ai_search_summary
        },
        timeout=30
    )
    response.raise_for_status()
    log_timing("Update Lead preferences", update_started)
    print("Lead preferences updated successfully", flush=True)


def merge_updated_preference_text(existing_text, generated_text, preference_update):
    existing_text = (existing_text or "").strip()
    generated_text = (generated_text or "").strip()
    preference_update = (preference_update or "").strip()
    empty_profile = re.search(
        r"\bno preferences? (?:provided|recorded|saved)(?: yet)?\b",
        existing_text,
        re.I
    )
    replacement_update = re.search(
        r"\b(?:no longer|instead|replace|remove|delete|change(?:d)?|only interested|"
        r"budget is now|now want|now need)\b",
        preference_update,
        re.I
    )

    if existing_text and not empty_profile and not replacement_update:
        # Additive updates are the common path. Preserve the stored profile byte-for-byte
        # (apart from non-preference placeholders) and append the new explicit requirement
        # rather than trusting a model rewrite.
        existing_text = "\n".join(
            line for line in existing_text.splitlines()
            if not re.match(
                r"^\s*(?:areas?|locations?|neighbou?rhoods?|other(?: notes)?)\s*:\s*"
                r"(?:no\b|none\b|not (?:provided|given|specified|recorded)\b)",
                line,
                re.I
            )
        ).strip()
        if preference_update.lower() in existing_text.lower():
            return existing_text
        return f"{existing_text}\n\nCustomer-stated preference update:\n{preference_update}"

    if preference_update and preference_update.lower() not in generated_text.lower():
        generated_text = (
            f"{generated_text}\n\nCustomer-stated preference update:\n"
            f"{preference_update}"
        ).strip()
    return preserve_unrelated_profile_lines(
        existing_text, generated_text, preference_update
    )


def _preference_categories(text):
    patterns = {
        "budget": r"\b(?:budget|rent|price|rm\s?\d)",
        "location": r"\b(?:area|location|condo|development|neighbou?rhood)\b",
        "bedrooms": r"\b(?:bedroom|\d+\s*bed)\b",
        "furnishing": r"\b(?:furnish|unfurnish|white goods)\b",
        "household": r"\b(?:household|family|adult|child|children|people|person)\b",
        "pets": r"\b(?:pet|cat|dog)\b",
        "school": r"\b(?:school|education)\b",
        "commute": r"\b(?:commute|workplace|office|transport|mrt|lrt)\b",
        "timing": r"\b(?:move[- ]?in|timing|date)\b",
        "property_type": r"\b(?:property type|condominium|landed|apartment)\b",
        "facilities": r"\b(?:facilit|pool|gym|balcony|view|car park|parking)\b",
    }
    lowered = text or ""
    return {
        category for category, pattern in patterns.items()
        if re.search(pattern, lowered, re.I)
    }


def preserve_unrelated_profile_lines(existing_text, updated_text, preference_update):
    """Fail closed against a rewrite silently dropping unrelated durable facts."""
    affected = _preference_categories(preference_update)
    updated = (updated_text or "").strip()
    updated_lower = updated.lower()
    preserved = []
    for line in (existing_text or "").splitlines():
        # Profiles may place several categories on one semicolon-delimited line.
        # Evaluate those clauses separately so changing the budget, for example,
        # cannot also discard an unrelated location from the same line.
        for fragment in re.split(r"\s*;\s*|\s+\|\s+", line):
            cleaned = fragment.strip()
            if not cleaned or cleaned.lower() in updated_lower:
                continue
            categories = _preference_categories(cleaned)
            if categories and categories & affected:
                continue
            if re.match(r"^home search requirements:?$", cleaned, re.I):
                continue
            preserved.append(cleaned)
    if preserved:
        updated = (
            f"{updated}\n\nPreserved existing requirements:\n"
            + "\n".join(preserved)
        ).strip()
    return updated


def preference_update_requires_rewrite(preference_update):
    return bool(re.search(
        r"\b(?:no longer|instead|replace|remove|delete|change(?:d)?|only interested|"
        r"budget is now|now want|now need)\b",
        preference_update or "",
        re.I
    ))


def generate_clean_preference_summary(updated_ai_search_text):
    response = client.responses.create(
        model="gpt-5-mini",
        input=updated_ai_search_text,
        instructions=(
            "Create a concise, clean, current renter preference summary from the "
            "durable home-search profile. Preserve all current requirements and "
            "meaningful qualifiers, ranges, quantities, and negatives. Exclude "
            "recommendation requests, conversational filler, history, secret notes, "
            "internal IDs, and implementation language. Remove duplication and obsolete "
            "replaced preferences. Return only the required JSON."
        ),
        text={
            "format": {
                "type": "json_schema",
                "name": "clean_renter_preference_summary",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"ai_search_summary": {"type": "string"}},
                    "required": ["ai_search_summary"],
                    "additionalProperties": False,
                },
            }
        },
    )
    summary = json.loads(response.output_text)["ai_search_summary"].strip()
    if not summary:
        raise ValueError("The generated AIsearchsummary was empty.")
    return summary


def update_preferences(folio_id, preference_update, bubble_env):

    preferences_started = time.perf_counter()
    base_url = get_bubble_base_url(bubble_env)
    print(f"Updating preferences for folio: {folio_id}", flush=True)
    folio_started = time.perf_counter()
    folio = bubble(f"{base_url}/obj/folio/{folio_id}")
    log_timing("update_preferences - Folio lookup", folio_started)
    lead_id = folio["lead"]
    print(f"Resolved lead: {lead_id}", flush=True)
    lead_started = time.perf_counter()
    lead = bubble(f"{base_url}/obj/lead/{lead_id}")
    log_timing("update_preferences - Lead lookup", lead_started)
    existing_ai_search_text = lead.get("AIsearchtext", "")

    if not preference_update_requires_rewrite(preference_update):
        updated_ai_search_text = merge_updated_preference_text(
            existing_ai_search_text,
            "Home search requirements:",
            preference_update
        )
        summary_started = time.perf_counter()
        ai_search_summary = generate_clean_preference_summary(
            updated_ai_search_text
        )
        log_timing("Preference summary cleanup OpenAI call", summary_started)
        update_lead_ai_searchtext(
            lead_id,
            updated_ai_search_text,
            ai_search_summary,
            base_url
        )
        log_timing("update_preferences TOTAL", preferences_started)
        return f"Got it — I’ve saved this preference: {preference_update}"

    update_prompt = f"""
You maintain a living home-search profile for one customer.

Return the complete updated AIsearchtext and a clean AIsearchsummary after
applying the requested update.

Rules:
- Preserve all existing relevant home-search information.
- Change or remove a preference only when the customer explicitly says to do so.
- Add relevant new information, creating an appropriate structured category when needed.
- Do not invent or infer preferences.
- Do not rewrite, summarise, clean up, reorder, or delete any `secret notes` or
  dated conversation/history content. It is immutable and must remain exactly
  as written.
- Do not summarise away, delete, or rewrite unrelated preferences.

AIsearchsummary rules:
- Generate it from the FINAL updated AIsearchtext, not only this latest request.
- It is a concise, customer-facing, easy-to-scan summary of current home-search
  preferences only.
- Include relevant current preferences where available, such as transaction type,
  budget, areas, condos, bedrooms, property type, furnishing, parking, schools,
  commute, family, facilities, and move-in requirements.
- Exclude secret notes, dated conversation/history content, internal IDs, internal
  implementation notes, and preferences that have been replaced.
- Use plain structured text with only non-empty categories. Do not use a table,
  generic introductory prose, or internal terminology.

CURRENT AIsearchtext:
{existing_ai_search_text}

REQUESTED PREFERENCE UPDATE:
{preference_update}
"""

    extraction_started = time.perf_counter()
    response = client.responses.create(
        model="gpt-5-mini",
        input=update_prompt,
        instructions=(
            "Return JSON matching the supplied schema. The confirmation must be a "
            "short, natural sentence addressed directly to the customer and must not "
            "mention internal IDs, fields, APIs, tools, matching mechanics, or future "
            "tool calls. It must not offer to contact agents or owners, arrange viewings, "
            "send photos, obtain floorplans, privately confirm availability, or perform "
            "exact commute checks. ai_search_summary must be "
            "a clean current customer-facing search summary derived from the final "
            "updated_ai_search_text."
        ),
        text={
            "format": {
                "type": "json_schema",
                "name": "updated_home_search_profile",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "updated_ai_search_text": {"type": "string"},
                        "ai_search_summary": {"type": "string"},
                        "confirmation": {"type": "string"}
                    },
                    "required": [
                        "updated_ai_search_text",
                        "ai_search_summary",
                        "confirmation"
                    ],
                    "additionalProperties": False
                }
            }
        }
    )
    log_timing("Preference extraction OpenAI call", extraction_started)
    log_token_usage("Preference", response)
    parse_started = time.perf_counter()
    result = json.loads(response.output_text)
    log_timing("update_preferences - parse result", parse_started)
    updated_ai_search_text = merge_updated_preference_text(
        existing_ai_search_text,
        result["updated_ai_search_text"],
        preference_update
    )

    if not updated_ai_search_text.strip():
        raise ValueError("The updated home-search profile was empty.")

    # Generate the concise summary from the guarded final profile. The rewrite
    # response may have omitted an unrelated requirement that the preservation
    # guard restored, so its draft summary is not authoritative.
    summary_started = time.perf_counter()
    ai_search_summary = generate_clean_preference_summary(
        updated_ai_search_text
    )
    log_timing("Preference summary cleanup OpenAI call", summary_started)

    lead_update_started = time.perf_counter()
    update_lead_ai_searchtext(
        lead_id,
        updated_ai_search_text,
        ai_search_summary,
        base_url
    )
    log_timing("update_preferences - Bubble Lead PATCH", lead_update_started)

    log_timing("update_preferences TOTAL", preferences_started)
    return result["confirmation"]


@app.route("/chat_stream", methods=["POST"])
def chat_stream():

    request_started = time.perf_counter()
    try:

        data = request.get_json(silent=True) or {}
        folio_id = data.get("folio_id")
        bubble_env = data.get("bubble_env", "live")

        if bubble_env not in ("development", "live"):
            bubble_env = "live"

        print(f"Bubble environment: {bubble_env}", flush=True)
        print(f"Folio ID received: {folio_id}", flush=True)
        message = next(
            (
                data.get(field)
                for field in ("message", "prompt", "user_message", "text")
                if isinstance(data.get(field), str) and data.get(field).strip()
            ),
            ""
        )

        previous = data.get("previous_response_id")

        if previous in ("", "null"):
            previous = None

        print(
            "Received /chat_stream request "
            f"keys={list(data.keys())} message={message[:160]!r} "
            f"has_previous={bool(previous)}",
            flush=True
        )

        if not message:
            log_timing("TOTAL REQUEST BEFORE ERROR", request_started)
            return jsonify({
                "error": (
                    "Missing chat message. Send it as 'message' (or prompt, "
                    "user_message, or text)."
                )
            }), 400

        @stream_with_context
        def generate():
            try:
                web_search_status_sent = False
                property_details_web_fallback = False
                first_delta_sent = False
                initial_first_event_logged = False
                initial_first_delta_logged = False
                renter_summary = load_front_door_renter_summary(
                    folio_id, bubble_env
                )

                def stream_initial_response(response_args, timing_label):
                    nonlocal initial_first_event_logged
                    nonlocal initial_first_delta_logged, web_search_status_sent

                    initial_started = time.perf_counter()
                    try:
                        with client.responses.stream(**response_args) as stream:
                            for event in stream:
                                if not initial_first_event_logged:
                                    log_timing(
                                        "Initial OpenAI FIRST EVENT",
                                        initial_started
                                    )
                                    initial_first_event_logged = True

                                if (
                                    event.type.startswith("response.web_search_call.")
                                    and not web_search_status_sent
                                ):
                                    print("Web search used", flush=True)
                                    yield (
                                        f"data: {json.dumps({'status': 'Searching the web for the latest information...'})}\n\n"
                                    )
                                    web_search_status_sent = True

                                if event.type == "response.output_text.delta":
                                    if not initial_first_delta_logged:
                                        log_timing(
                                            "Initial OpenAI FIRST DELTA",
                                            initial_started
                                        )
                                        initial_first_delta_logged = True
                                    # This response selects tools. Do not expose provisional
                                    # model text before knowing whether it contains a tool call.

                            final_response = stream.get_final_response()
                            log_token_usage("Initial", final_response)
                    except Exception:
                        log_timing(f"{timing_label} failed", initial_started)
                        raise

                    log_timing(f"{timing_label} complete", initial_started)
                    return final_response

                # The initial turn carries the incoming response ID, preserving
                # the user's existing conversation history.
                try:
                    response = yield from stream_initial_response(
                        build_response_args(message, previous, renter_summary),
                        "Initial OpenAI/tool selection"
                    )
                except Exception as error:
                    if "No tool output found for function call" not in str(error):
                        raise

                    print(
                        "Broken previous_response_id detected; starting a fresh conversation",
                        flush=True
                    )
                    response = yield from stream_initial_response(
                        build_response_args(message, None, renter_summary),
                        "Initial OpenAI/tool selection retry"
                    )
                if any(
                    output_item.type == "web_search_call"
                    for output_item in response.output
                ) and not web_search_status_sent:
                    print("Web search used", flush=True)
                    yield (
                        f"data: {json.dumps({'status': 'Searching the web for the latest information...'})}\n\n"
                    )
                    web_search_status_sent = True
                tool_call = next(
                    (x for x in response.output if x.type == "function_call"),
                    None
                )

                if tool_call is None:
                    print("No tool call requested", flush=True)
                    if response.output_text:
                        if not first_delta_sent:
                            log_timing("FIRST DELTA", request_started)
                            first_delta_sent = True
                        yield f"data: {json.dumps({'delta': response.output_text})}\n\n"
                    citations = get_web_citations(response)

                    if citations:
                        yield f"data: {json.dumps({'citations': citations})}\n\n"
                    log_timing("TOTAL REQUEST", request_started)
                    yield (
                        f"data: {json.dumps({'done': True, 'response_id': response.id})}\n\n"
                    )
                    return

                original_response_id = response.id
                original_call_id = tool_call.call_id
                print(f"Tool selected: {tool_call.name}", flush=True)
                print(f"Original call_id: {original_call_id}", flush=True)
                tool_args = json.loads(tool_call.arguments)
                follow_up_tools = None
                direct_customer_response = None

                if tool_call.name == "match_lead":
                    tool_result = yield from stream_match_lead(
                        folio_id, bubble_env, message
                    )
                    has_match_results = True
                    # match_lead already returns the final answer generated solely from
                    # the current listing snapshot. Send that grounded answer immediately;
                    # the continuation is still completed below to preserve the Responses
                    # API chain and its reusable previous_response_id.
                    direct_customer_response = tool_result
                    follow_up_instructions = (
                        "The tool output already contains the final customer-facing answer. "
                        "Return it faithfully. Do not add, remove, reinterpret, embellish, "
                        "or invent property information. Do not mention tools or matching "
                        "mechanics and do not offer to contact agents or owners, arrange "
                        "viewings, send photos, obtain floorplans, or privately check facts."
                    )
                elif tool_call.name == "update_preferences":
                    yield (
                        f"data: {json.dumps({'status': 'Updating your preferences...'})}\n\n"
                    )
                    # Persist only the lasting requirement extracted by the router. The
                    # raw message still carries current intent and temporary search scope.
                    preference_text = str(
                        tool_args.get("preference_update") or ""
                    ).strip()
                    if not preference_text:
                        raise ValueError(
                            "Preference update tool call contained no lasting preference."
                        )
                    preference_confirmation = update_preferences(
                        folio_id,
                        preference_text,
                        bubble_env
                    )
                    if tool_args.get("recommendations_requested") is True:
                        print(
                            "Preference update complete; recommendation request requires rematch",
                            flush=True
                        )
                        yield (
                            f"data: {json.dumps({'status': 'Preferences updated — refreshing your recommendations...'})}\n\n"
                        )
                        recommendations = yield from stream_match_lead(
                            folio_id,
                            bubble_env,
                            message
                        )
                        print("Automatic rematch complete", flush=True)
                        has_match_results = True
                        tool_result = (
                            "Absolutely — I've updated your preferences. Based on that, "
                            "here's what I'd recommend now:\n\n"
                            f"{recommendations}"
                        )
                        direct_customer_response = tool_result
                        follow_up_instructions = (
                            "The tool output already contains the final customer-facing "
                            "recommendations. Return it faithfully without adding, removing, "
                            "or inventing property information. Do not mention tools or matching "
                            "mechanics and do not offer to contact agents or owners, arrange "
                            "viewings, send photos, obtain floorplans, or privately check facts."
                        )
                    else:
                        has_match_results = False
                        tool_result = preference_confirmation
                        direct_customer_response = preference_confirmation
                        follow_up_instructions = (
                            "Return the completed preference-update confirmation naturally. "
                            "Do not mention properties, tools, matching mechanics, or internal "
                            "errors. Do not ask follow-up questions or offer to contact agents "
                            "or owners, arrange viewings, send photos, obtain floorplans, or "
                            "privately check facts."
                        )
                elif tool_call.name == "get_property_details":
                    has_match_results = False
                    yield (
                        f"data: {json.dumps({'status': 'Checking property details...'})}\n\n"
                    )
                    tool_result = get_property_details(
                        folio_id,
                        tool_args["property_reference"],
                        bubble_env
                    )
                    follow_up_instructions = (
                        "Check whether the authoritative Rentee details in the tool output "
                        "actually answer the customer's question. If a requested general "
                        "building, development, location, neighbourhood, transport, school, "
                        "amenity, developer, historical, regulatory, or other public external "
                        "fact is missing, use web search immediately before answering. If a "
                        "missing fact is specific to this available unit, do not search or "
                        "guess; say the current listing information does not specify it. Do "
                        "not expose internal identifiers or offer unsupported actions."
                    )
                    # The model decides whether the returned details are incomplete
                    # and whether a public web lookup is appropriate. The streamed
                    # web-search event below then selects the customer-facing status.
                    property_details_web_fallback = True
                    follow_up_tools = [{"type": "web_search"}]
                elif tool_call.name == "get_condo_info":
                    has_match_results = False
                    yield (
                        f"data: {json.dumps({'status': 'Checking condo information...'})}\n\n"
                    )
                    condo_names = tool_args.get("condo_names")
                    if not isinstance(condo_names, list):
                        condo_names = []
                    tool_result = get_condo_infos(condo_names)
                    follow_up_instructions = (
                        "Answer the customer's condo question using the supplied condo data. "
                        "Use factual fields as facts. Treat Persona as qualitative expert "
                        "insight and phrase opinions, suitability, strengths, weaknesses, and "
                        "trade-offs accordingly. For comparisons, compare only the returned "
                        "data. Clearly identify condos that were not found and say when the "
                        "requested information is unavailable. Do not invent missing details, "
                        "claim current listing availability, or expose tool/internal field names. "
                        "Do not offer to contact agents or owners, arrange viewings, send photos, "
                        "obtain floorplans, or privately check facts."
                    )
                else:
                    raise ValueError(f"Unsupported tool: {tool_call.name}")

                if has_match_results:
                    yield (
                        f"data: {json.dumps({'status': 'Found some options — putting them together...'})}\n\n"
                    )

                if direct_customer_response:
                    if not first_delta_sent:
                        log_timing("FIRST DELTA", request_started)
                        first_delta_sent = True
                    yield f"data: {json.dumps({'delta': direct_customer_response})}\n\n"

                # Continue the same response chain with the function result,
                # then stream the final assistant answer back to Bubble.
                if tool_call.name == "update_preferences":
                    print(
                        "Submitting function_call_output for original "
                        f"update_preferences call {original_call_id}",
                        flush=True
                    )
                elif tool_call.name == "match_lead":
                    print(
                        "Submitting function_call_output for original "
                        f"match_lead call {original_call_id}",
                        flush=True
                    )
                elif tool_call.name == "get_condo_info":
                    print(
                        "Submitting function_call_output for original "
                        f"get_condo_info call {original_call_id}",
                        flush=True
                    )
                else:
                    print(
                        "Submitting function_call_output for original "
                        f"get_property_details call {original_call_id}",
                        flush=True
                    )
                continuation_args = {
                    "model": "gpt-5-mini",
                    "previous_response_id": original_response_id,
                    "instructions": follow_up_instructions,
                    "input": [{
                        "type": "function_call_output",
                        "call_id": original_call_id,
                        "output": tool_result
                    }]
                }

                if follow_up_tools:
                    continuation_args["tools"] = follow_up_tools
                else:
                    continuation_args["tools"] = []

                final_openai_started = time.perf_counter()
                with client.responses.stream(**continuation_args) as stream:
                    for event in stream:
                        if (
                            event.type.startswith("response.web_search_call.")
                            and not web_search_status_sent
                        ):
                            print("Web search used", flush=True)
                            status = (
                                "That detail isn’t in the listing — checking the web..."
                                if property_details_web_fallback
                                else "Searching the web for the latest information..."
                            )
                            yield (
                                f"data: {json.dumps({'status': status})}\n\n"
                            )
                            web_search_status_sent = True
                        if (
                            event.type == "response.output_text.delta"
                            and direct_customer_response is None
                        ):
                            if not first_delta_sent:
                                log_timing("FIRST DELTA", request_started)
                                first_delta_sent = True
                            yield f"data: {json.dumps({'delta': event.delta})}\n\n"

                    final = stream.get_final_response()
                log_token_usage("Final", final)
                log_timing("Final OpenAI completion", final_openai_started)

                unresolved_calls = [
                    item for item in final.output if item.type == "function_call"
                ]
                if unresolved_calls:
                    raise RuntimeError(
                        "OpenAI continuation returned an unresolved function call; "
                        "the response_id will not be exposed for reuse."
                    )

                print("Tool lifecycle completed", flush=True)

                citations = get_web_citations(final)

                if citations:
                    yield f"data: {json.dumps({'citations': citations})}\n\n"

                log_timing("TOTAL REQUEST", request_started)
                yield (
                    f"data: {json.dumps({'done': True, 'response_id': final.id})}\n\n"
                )
            except Exception as error:
                print(f"/chat_stream failed: {error}", flush=True)
                log_timing("TOTAL REQUEST BEFORE ERROR", request_started)
                yield f"data: {json.dumps({'error': str(error), 'done': True})}\n\n"

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    except Exception as e:

        log_timing("TOTAL REQUEST BEFORE ERROR", request_started)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
