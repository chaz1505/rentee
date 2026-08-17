import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from openai import OpenAI

try:
    from .bubble_test_data import get_bubble_base
    from .evaluate_run import evaluate_run
    from .generate_fix_prompt import generate_fix_prompt
    from .generate_evaluation_markdown import generate_evaluation_markdown
    from .generate_conversation_markdown import generate_conversation_markdown
    from .save_benchmark_run import (
        get_previous_benchmark_run,
        save_benchmark_run,
    )
    from .test_subject import (
        ensure_test_subject,
        reset_test_subject,
        snapshot_test_subject,
    )
except ImportError:
    from bubble_test_data import get_bubble_base
    from evaluate_run import evaluate_run
    from generate_fix_prompt import generate_fix_prompt
    from generate_evaluation_markdown import generate_evaluation_markdown
    from generate_conversation_markdown import generate_conversation_markdown
    from save_benchmark_run import (
        get_previous_benchmark_run,
        save_benchmark_run,
    )
    from test_subject import ensure_test_subject, reset_test_subject, snapshot_test_subject


DEFAULT_STREAM_URL = "https://rentee-2.onrender.com/chat_stream"
STREAM_TIMEOUT = (30, 300)
TESTS_DIR = os.path.dirname(__file__)
SYNTHETIC_USER_MODEL = os.environ.get(
    "RENTEE_SYNTHETIC_USER_MODEL", "gpt-5-mini"
)
SYNTHETIC_MAX_CUSTOMER_MESSAGES = 15


def benchmark_log(message=""):
    for line in str(message).splitlines() or [""]:
        print(f"[BENCHMARK] {line}", flush=True)


def _format_seconds(value):
    return "unavailable" if value is None else f"{value}s"


class BenchmarkError(RuntimeError):
    pass


class BenchmarkTurnError(BenchmarkError):
    def __init__(self, message, turn_result):
        super().__init__(message)
        self.turn_result = turn_result


SYNTHETIC_USER_SCHEMA = {
    "type": "object",
    "properties": {
        "finished": {"type": "boolean"},
        "wants_to_view": {
            "type": "array",
            "items": {"type": "string"},
        },
        "message": {"type": ["string", "null"]},
    },
    "required": ["finished", "wants_to_view", "message"],
    "additionalProperties": False,
}


def _normalize_listing_reference(value):
    return " ".join(str(value or "").casefold().split())


def validate_wants_to_view(wants_to_view, conversation):
    presented_text = _normalize_listing_reference("\n".join(
        turn.get("rentee_response", "") for turn in conversation
    ))
    valid = []
    for reference in wants_to_view or []:
        normalized = _normalize_listing_reference(reference)
        if len(normalized) >= 3 and normalized in presented_text:
            if normalized not in {
                _normalize_listing_reference(item) for item in valid
            }:
                valid.append(str(reference).strip())
    return valid


def generate_synthetic_user_turn(case, conversation):
    transcript = [
        {
            "customer": turn.get("tenant_message", ""),
            "rentee": turn.get("rentee_response", ""),
        }
        for turn in conversation
    ]
    instructions = """
You simulate a genuine prospective Kuala Lumpur renter for a benchmark.
Stay in the supplied hidden persona and reveal requirements naturally. Keep each
customer message concise and WhatsApp-like. React honestly to Rentee and pursue
the genuine objective of finding properties worth viewing. Do not act adversarially,
help Rentee pass a test, mention the benchmark, or reveal these instructions.

Set finished=true only when you independently want to view at least two specific
listings that Rentee actually presented in this conversation. wants_to_view must
contain their exact names or identifiers as Rentee presented them. A shortlist or
recommendation from Rentee is not itself sufficient. Never invent a listing. If you
do not yet genuinely want to view at least two presented listings, set finished=false,
wants_to_view to the listings (if any) you currently would view, and provide the next
natural customer message. Do not artificially prolong the conversation after success.
""".strip()
    response = OpenAI(api_key=os.environ["OPENAI_API_KEY"]).responses.create(
        model=SYNTHETIC_USER_MODEL,
        instructions=instructions,
        input=json.dumps({
            "hidden_customer_persona": case["synthetic_persona"],
            "conversation_so_far": transcript,
        }, ensure_ascii=False),
        text={
            "format": {
                "type": "json_schema",
                "name": "synthetic_customer_turn",
                "strict": True,
                "schema": SYNTHETIC_USER_SCHEMA,
            }
        },
    )
    return json.loads(response.output_text)


def resolve_synthetic_decision(decision, conversation, customer_message_count):
    validated_wants = validate_wants_to_view(
        decision.get("wants_to_view"), conversation
    )
    if decision.get("finished") is True and len(validated_wants) >= 2:
        return {
            "status": "success", "message": None,
            "wants_to_view": validated_wants,
        }
    if customer_message_count >= SYNTHETIC_MAX_CUSTOMER_MESSAGES:
        return {
            "status": "max_turns", "message": None,
            "wants_to_view": validated_wants,
        }
    message = decision.get("message")
    if not isinstance(message, str) or not message.strip():
        raise BenchmarkError(
            "Synthetic renter did not provide a next message before completion."
        )
    return {
        "status": "continue", "message": message.strip(),
        "wants_to_view": validated_wants,
    }


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iter_sse_events(response):
    data_lines = []
    for raw_line in response.iter_lines(decode_unicode=True):
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        line = line or ""
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)


def run_turn(message, folio_id, previous_response_id=None, environment="development"):
    stream_url = os.environ.get("RENTEE_STREAM_URL", DEFAULT_STREAM_URL)
    payload = {
        "message": message,
        "previous_response_id": previous_response_id,
        "folio_id": folio_id,
        "bubble_env": environment
    }
    started = time.perf_counter()
    response = requests.post(
        stream_url,
        json=payload,
        stream=True,
        timeout=STREAM_TIMEOUT
    )
    if not response.ok:
        raise BenchmarkError(
            f"/chat_stream returned HTTP {response.status_code}: {response.text}"
        )

    result = {
        "text": "",
        "response_id": None,
        "previous_response_id_sent": previous_response_id,
        "statuses": [],
        "citations": [],
        "errors": [],
        "timing": {
            "first_event_s": None,
            "first_delta_s": None,
            "total_s": None
        },
        "done_seen": False
    }

    for event_text in _iter_sse_events(response):
        elapsed = round(time.perf_counter() - started, 3)
        if result["timing"]["first_event_s"] is None:
            result["timing"]["first_event_s"] = elapsed
        try:
            event = json.loads(event_text)
        except json.JSONDecodeError as error:
            raise BenchmarkError(f"Invalid SSE JSON event: {event_text!r}") from error

        if "status" in event:
            result["statuses"].append({"at_s": elapsed, "status": event["status"]})
        if "delta" in event:
            if result["timing"]["first_delta_s"] is None:
                result["timing"]["first_delta_s"] = elapsed
            result["text"] += str(event["delta"])
        if "citations" in event:
            citations = event["citations"]
            result["citations"].extend(citations if isinstance(citations, list) else [citations])
        if event.get("response_id"):
            result["response_id"] = event["response_id"]
        if event.get("error"):
            result["errors"].append(event["error"])
        if event.get("done") is True:
            result["done_seen"] = True
            result["timing"]["total_s"] = elapsed
            break

    if result["errors"]:
        raise BenchmarkTurnError(
            f"SSE returned an error event: {result['errors']}", result
        )
    if not result["done_seen"]:
        raise BenchmarkTurnError("SSE stream ended without done=true", result)
    if not result["response_id"]:
        raise BenchmarkTurnError(
            "No response_id returned; conversation continuity cannot be verified", result
        )
    return result


def _load_cases():
    path = os.path.join(TESTS_DIR, "benchmark_cases.json")
    with open(path, "r", encoding="utf-8") as cases_file:
        return json.load(cases_file)


def _save_result(case_id, result, environment="development"):
    results_dir = os.path.join(TESTS_DIR, "results")
    os.makedirs(results_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(
        results_dir, f"{case_id}_{environment}_{timestamp}.json"
    )
    with open(path, "w", encoding="utf-8") as result_file:
        json.dump(result, result_file, indent=2, ensure_ascii=False)
        result_file.write("\n")
    return path


def run_case(case, run_id=None, progress_callback=None, environment="development"):
    # Force development-base validation before any Bubble request or chat turn.
    get_bubble_base(environment)
    if not run_id:
        run_id = (
            f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_"
            f"{case['id']}_{environment}"
        )
    benchmark_log("=" * 40)
    benchmark_log("Starting benchmark run")
    if run_id:
        benchmark_log(f"Run ID: {run_id}")
    benchmark_log(f"Case: {case['name']}")
    benchmark_log(f"Environment: {environment.upper()}")
    benchmark_log("=" * 40)
    benchmark_log("Resetting Bubble test subject...")
    subject = ensure_test_subject(case, environment)
    reset_test_subject(case, subject, environment)
    benchmark_log("Reset complete.")
    benchmark_log(f"Using Lead: {subject['lead_id']}")
    benchmark_log(f"Using Folio: {subject['folio_id']}")

    result = {
        "run_id": run_id,
        "case_id": case["id"],
        "case_name": case["name"],
        "started_at_utc": _utc_now(),
        "stream_url": os.environ.get("RENTEE_STREAM_URL", DEFAULT_STREAM_URL),
        "bubble_env": environment,
        "subject": subject,
        "initial_state": snapshot_test_subject(subject, environment),
        "turns": [],
        "final_state": {},
        "completed_at_utc": None,
        "failure": None
    }
    synthetic = case.get("conversation_mode") == "synthetic"
    if synthetic:
        result["synthetic_completion"] = {
            "status": "running",
            "wants_to_view": [],
            "customer_messages": 0,
        }
    previous_response_id = None
    turn_number = 0
    while True:
        if synthetic:
            decision = generate_synthetic_user_turn(case, result["turns"])
            synthetic_action = resolve_synthetic_decision(
                decision, result["turns"], turn_number
            )
            if synthetic_action["status"] == "success":
                result["synthetic_completion"] = {
                    "status": "success",
                    "wants_to_view": synthetic_action["wants_to_view"],
                    "customer_messages": turn_number,
                }
                benchmark_log(
                    "Synthetic renter completed with at least two viewings."
                )
                break
            if synthetic_action["status"] == "max_turns":
                result["synthetic_completion"] = {
                    "status": "max_turns",
                    "wants_to_view": synthetic_action["wants_to_view"],
                    "customer_messages": turn_number,
                }
                benchmark_log("Synthetic renter reached the 15-message ceiling.")
                break
            message = synthetic_action["message"]
            turn_number += 1
        else:
            if turn_number >= len(case["turns"]):
                break
            message = case["turns"][turn_number]["message"]
            turn_number += 1
        if progress_callback:
            progress_callback({"case": case["id"], "current_turn": turn_number})
        benchmark_log("")
        benchmark_log(f"TURN {turn_number}")
        benchmark_log(f"Tenant: {message}")
        try:
            turn_result = run_turn(
                message, subject["folio_id"], previous_response_id, environment
            )
        except BenchmarkTurnError as error:
            turn_result = error.turn_result
            result["failure"] = {
                "turn": turn_number,
                "type": "stream_or_conversation_chain",
                "message": str(error)
            }
        except Exception as error:
            turn_result = {
                "text": "",
                "response_id": None,
                "previous_response_id_sent": previous_response_id,
                "statuses": [],
                "citations": [],
                "errors": [str(error)],
                "timing": {
                    "first_event_s": None,
                    "first_delta_s": None,
                    "total_s": None
                },
                "done_seen": False
            }
            result["failure"] = {
                "turn": turn_number,
                "type": "request_failure",
                "message": str(error)
            }
        previous_response_id = turn_result["response_id"]
        timing = turn_result["timing"]
        benchmark_log(f"Turn {turn_number} first event: {_format_seconds(timing['first_event_s'])}")
        benchmark_log(f"Turn {turn_number} first text: {_format_seconds(timing['first_delta_s'])}")
        benchmark_log(f"Turn {turn_number} total: {_format_seconds(timing['total_s'])}")
        benchmark_log("")
        benchmark_log("Rentee:")
        benchmark_log(turn_result["text"])
        result["turns"].append({
            "turn": turn_number,
            "tenant_message": message,
            "rentee_response": turn_result["text"],
            "response_id": turn_result["response_id"],
            "previous_response_id_sent": turn_result["previous_response_id_sent"],
            "statuses": turn_result["statuses"],
            "citations": turn_result["citations"],
            "errors": turn_result["errors"],
            "timing": turn_result["timing"],
            "done_seen": turn_result["done_seen"]
        })
        if result["failure"]:
            benchmark_log("")
            benchmark_log(f"Failure: Turn {turn_number} — {result['failure']['message']}")
            break

    try:
        result["final_state"] = snapshot_test_subject(subject, environment)
    except Exception as error:
        if result["failure"] is None:
            result["failure"] = {
                "turn": None,
                "type": "bubble_state_snapshot",
                "message": f"Final Bubble state snapshot failed: {error}"
            }
        else:
            result["failure"]["final_state_error"] = str(error)
    result["completed_at_utc"] = _utc_now()
    output_path = _save_result(case["id"], result, environment)
    conversation_path = None
    conversation_markdown = None
    conversation_artifact_error = None
    try:
        conversation_path = generate_conversation_markdown(
            result, case, os.path.dirname(os.path.abspath(output_path))
        )
        with open(conversation_path, "r", encoding="utf-8") as source:
            conversation_markdown = source.read()
    except Exception as error:
        conversation_artifact_error = str(error)
        benchmark_log(
            f"Conversation artifact generation failed: {error}"
        )
    previous_benchmark_run = None
    try:
        previous_benchmark_run = get_previous_benchmark_run(
            case["id"], environment, run_id
        )
    except Exception as error:
        benchmark_log(f"Bubble previous-run lookup unavailable: {error}")
    evaluation_path, evaluation = evaluate_run(
        output_path, previous_benchmark_run=previous_benchmark_run
    )
    evaluation_markdown_path = generate_evaluation_markdown(
        output_path, evaluation_path
    )
    prompt_path = generate_fix_prompt(output_path, evaluation_path)
    with open(evaluation_markdown_path, "r", encoding="utf-8") as source:
        evaluation_markdown = source.read()
    with open(prompt_path, "r", encoding="utf-8") as source:
        fix_prompt = source.read()
    persistence = {"persisted": False, "benchmark_run_id": None, "error": None}
    try:
        persistence["benchmark_run_id"] = save_benchmark_run(
            result, evaluation, evaluation_markdown, fix_prompt, environment,
            conversation_markdown=conversation_markdown,
        )
        persistence["persisted"] = True
    except Exception as error:
        persistence["error"] = str(error)

    benchmark_log("")
    benchmark_log("=" * 40)
    benchmark_log("BENCHMARK FAILED" if result["failure"] else "BENCHMARK COMPLETE")
    benchmark_log(f"Environment: {environment.upper()}")
    benchmark_log(f"Status: {evaluation['overall_status'].upper()}")
    benchmark_log(
        f"Average first text: "
        f"{_format_seconds(evaluation.get('metrics', {}).get('average_first_delta_s'))}"
    )
    benchmark_log(
        f"Average total: "
        f"{_format_seconds(evaluation.get('metrics', {}).get('average_total_s'))}"
    )
    first_text_values = [
        turn.get("timing", {}).get("first_delta_s") for turn in result.get("turns", [])
    ]
    first_text_values = [value for value in first_text_values if isinstance(value, (int, float))]
    benchmark_log(f"Max first text: {_format_seconds(max(first_text_values) if first_text_values else None)}")
    important = [
        issue for issue in evaluation.get("issues", [])
        if issue.get("severity") in ("critical", "high")
    ]
    if important:
        benchmark_log("")
        benchmark_log("Critical/high issues:")
        for issue in important:
            benchmark_log(f"- {issue['id'].replace('_', ' ').title()}")
    comparison = evaluation.get("comparison_to_previous_run", {})
    benchmark_log("")
    benchmark_log("Compared with previous run:")
    if comparison.get("available"):
        benchmark_log(f"First-text latency: {comparison.get('first_delta_change_pct')}%")
        unsupported = comparison.get("unsupported_actions", {})
        benchmark_log(
            f"Unsupported actions: {unsupported.get('previous')} -> "
            f"{unsupported.get('current')}"
        )
    else:
        benchmark_log("No previous run available.")
    benchmark_log("")
    benchmark_log(f"Raw result: {output_path}")
    if conversation_path:
        benchmark_log(f"Conversation: {conversation_path}")
    benchmark_log(f"Evaluation JSON: {evaluation_path}")
    benchmark_log(f"Human evaluation: {evaluation_markdown_path}")
    benchmark_log(f"Codex fix prompt: {prompt_path}")
    benchmark_log(
        "Bubble persistence: SUCCESS" if persistence["persisted"]
        else "Bubble persistence: FAILED"
    )
    if persistence["persisted"]:
        benchmark_log(f"BenchmarkRun Bubble ID: {persistence['benchmark_run_id']}")
    else:
        benchmark_log(persistence["error"] or "Unknown persistence error")
        benchmark_log("Local benchmark artifacts remain available on Render.")
    benchmark_log(f"Run ID: {run_id}")
    benchmark_log("=" * 40)
    result["execution"] = {
        "run_id": run_id,
        "benchmark_status": evaluation["overall_status"],
        "result_path": output_path,
        "conversation_path": conversation_path,
        "conversation_artifact_error": conversation_artifact_error,
        "evaluation_path": evaluation_path,
        "evaluation_markdown_path": evaluation_markdown_path,
        "fix_prompt_path": prompt_path,
        "benchmark_run_id": persistence["benchmark_run_id"],
        "result_persisted": persistence["persisted"],
        "persistence_error": persistence["error"]
    }
    return result


def _default_cases():
    return [
        case for case in _load_cases()
        if case.get("conversation_mode") == "synthetic"
    ]


def get_benchmark_case_ids(include_scripted=False):
    cases = _load_cases() if include_scripted else _default_cases()
    return [case["id"] for case in cases]


def _select_cases(case_ids=None):
    if case_ids is None:
        return _default_cases()
    requested = list(case_ids)
    cases_by_id = {case["id"]: case for case in _load_cases()}
    missing = [case_id for case_id in requested if case_id not in cases_by_id]
    if missing:
        raise BenchmarkError(
            "Unknown benchmark case ID(s): " + ", ".join(missing)
        )
    return [cases_by_id[case_id] for case_id in requested]


def _record_infrastructure_error(case, run_id, environment, error):
    now = _utc_now()
    result = {
        "run_id": run_id,
        "case_id": case["id"],
        "case_name": case["name"],
        "started_at_utc": now,
        "completed_at_utc": now,
        "bubble_env": environment,
        "turns": [],
        "initial_state": {},
        "final_state": {},
        "failure": {"turn": None, "type": "infrastructure", "message": str(error)},
        "infrastructure_error": str(error),
    }
    output_path = _save_result(case["id"], result, environment)
    conversation_path = None
    conversation_markdown = None
    conversation_artifact_error = None
    try:
        conversation_path = generate_conversation_markdown(
            result, case, os.path.dirname(os.path.abspath(output_path))
        )
        with open(conversation_path, "r", encoding="utf-8") as source:
            conversation_markdown = source.read()
    except Exception as artifact_error:
        conversation_artifact_error = str(artifact_error)
        benchmark_log(
            f"Conversation artifact generation failed: {artifact_error}"
        )
    evaluation_path, evaluation = evaluate_run(
        output_path, previous_benchmark_run=None
    )
    evaluation_markdown_path = generate_evaluation_markdown(
        output_path, evaluation_path
    )
    prompt_path = generate_fix_prompt(output_path, evaluation_path)
    with open(evaluation_markdown_path, "r", encoding="utf-8") as source:
        evaluation_markdown = source.read()
    with open(prompt_path, "r", encoding="utf-8") as source:
        fix_prompt = source.read()
    persisted, benchmark_run_id, persistence_error = False, None, None
    try:
        benchmark_run_id = save_benchmark_run(
            result, evaluation, evaluation_markdown, fix_prompt, environment,
            conversation_markdown=conversation_markdown,
        )
        persisted = True
    except Exception as persistence_exception:
        persistence_error = str(persistence_exception)
        benchmark_log("Bubble persistence: FAILED")
        benchmark_log(persistence_error)
        benchmark_log("Local benchmark artifacts remain available on Render.")
    return dict(result, execution={
        "run_id": run_id,
        "benchmark_status": "error",
        "result_path": output_path,
        "conversation_path": conversation_path,
        "conversation_artifact_error": conversation_artifact_error,
        "evaluation_path": evaluation_path,
        "evaluation_markdown_path": evaluation_markdown_path,
        "fix_prompt_path": prompt_path,
        "benchmark_run_id": benchmark_run_id,
        "result_persisted": persisted,
        "persistence_error": persistence_error,
    })


def validate_benchmark_environment(environment):
    if environment not in ("development", "live"):
        raise BenchmarkError(f"Unsupported benchmark environment: {environment}")
    if (
        environment == "live"
        and os.environ.get("BENCHMARK_LIVE_ENABLED", "").strip().lower() != "true"
    ):
        raise BenchmarkError(
            "Live benchmark is disabled; set BENCHMARK_LIVE_ENABLED=true explicitly."
        )
    get_bubble_base(environment)
    return environment


def run_all_benchmarks(
    run_id=None, progress_callback=None, environment="development",
    case_ids=None,
):
    validate_benchmark_environment(environment)
    results = []
    for case in _select_cases(case_ids):
        case_run_id = run_id or (
            f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_"
            f"{case['id']}_{environment}"
        )
        try:
            results.append(run_case(
                case,
                run_id=case_run_id,
                progress_callback=progress_callback,
                environment=environment
            ))
        except Exception as error:
            benchmark_log(f"Benchmark infrastructure error: {error}")
            results.append(_record_infrastructure_error(
                case, case_run_id, environment, error
            ))
    return {
        "run_id": run_id,
        "results": results,
        "failed": any(bool(result.get("failure")) for result in results)
    }


def main():
    failed = False
    try:
        environment = os.environ.get("BENCHMARK_ENVIRONMENT", "development")
        selected_case_id = os.environ.get("BENCHMARK_CASE_ID", "").strip()
        if selected_case_id:
            suite = run_all_benchmarks(
                environment=environment,
                case_ids=[selected_case_id],
            )
        else:
            suite = run_all_benchmarks(environment=environment)
        failed = suite["failed"]
    except Exception as error:
        print(f"BENCHMARK FAILED: {error}", file=sys.stderr, flush=True)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
