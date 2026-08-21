import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

try:
    from .bubble_test_data import get_bubble_dev_base
    from .evaluate_run import evaluate_run
    from .generate_fix_prompt import generate_fix_prompt
    from .publish_results import publish_benchmark_results
    from .test_subject import (
        ensure_test_subject,
        reset_test_subject,
        snapshot_test_subject,
    )
except ImportError:
    from bubble_test_data import get_bubble_dev_base
    from evaluate_run import evaluate_run
    from generate_fix_prompt import generate_fix_prompt
    from publish_results import publish_benchmark_results
    from test_subject import ensure_test_subject, reset_test_subject, snapshot_test_subject


DEFAULT_STREAM_URL = "https://rentee-2.onrender.com/chat_stream"
STREAM_TIMEOUT = (30, 300)
TESTS_DIR = os.path.dirname(__file__)


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


def run_turn(message, folio_id, previous_response_id=None):
    stream_url = os.environ.get("RENTEE_STREAM_URL", DEFAULT_STREAM_URL)
    payload = {
        "message": message,
        "previous_response_id": previous_response_id,
        "folio_id": folio_id,
        "bubble_env": "development"
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


def _save_result(case_id, result):
    results_dir = os.path.join(TESTS_DIR, "results")
    os.makedirs(results_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(results_dir, f"{case_id}_{timestamp}.json")
    with open(path, "w", encoding="utf-8") as result_file:
        json.dump(result, result_file, indent=2, ensure_ascii=False)
        result_file.write("\n")
    return path


def run_case(case, run_id=None, progress_callback=None):
    # Force development-base validation before any Bubble request or chat turn.
    get_bubble_dev_base()
    benchmark_log("=" * 40)
    benchmark_log("Starting benchmark run")
    if run_id:
        benchmark_log(f"Run ID: {run_id}")
    benchmark_log(f"Case: {case['name']}")
    benchmark_log("=" * 40)
    benchmark_log("Resetting Bubble test subject...")
    subject = ensure_test_subject(case)
    reset_test_subject(case, subject)
    benchmark_log("Reset complete.")
    benchmark_log(f"Using Lead: {subject['lead_id']}")
    benchmark_log(f"Using Folio: {subject['folio_id']}")

    result = {
        "case_id": case["id"],
        "case_name": case["name"],
        "started_at_utc": _utc_now(),
        "stream_url": os.environ.get("RENTEE_STREAM_URL", DEFAULT_STREAM_URL),
        "bubble_env": "development",
        "subject": subject,
        "initial_state": snapshot_test_subject(subject),
        "turns": [],
        "final_state": {},
        "completed_at_utc": None,
        "failure": None
    }
    previous_response_id = None
    for turn_number, scripted_turn in enumerate(case["turns"], start=1):
        message = scripted_turn["message"]
        if progress_callback:
            progress_callback({"case": case["id"], "current_turn": turn_number})
        benchmark_log("")
        benchmark_log(f"TURN {turn_number}")
        benchmark_log(f"Tenant: {message}")
        try:
            turn_result = run_turn(message, subject["folio_id"], previous_response_id)
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
        result["final_state"] = snapshot_test_subject(subject)
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
    output_path = _save_result(case["id"], result)
    evaluation_path, evaluation = evaluate_run(output_path)
    prompt_path = generate_fix_prompt(output_path, evaluation_path)
    publishing_result = publish_benchmark_results([
        output_path, evaluation_path, prompt_path
    ])

    benchmark_log("")
    benchmark_log("=" * 40)
    benchmark_log("BENCHMARK FAILED" if result["failure"] else "BENCHMARK COMPLETE")
    benchmark_log(f"Status: {evaluation['overall_status'].upper()}")
    benchmark_log(
        f"Average first text: "
        f"{_format_seconds(evaluation.get('metrics', {}).get('average_first_delta_s'))}"
    )
    benchmark_log(
        f"Average total: "
        f"{_format_seconds(evaluation.get('metrics', {}).get('average_total_s'))}"
    )
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
    benchmark_log(f"Evaluation: {evaluation_path}")
    benchmark_log(f"Codex fix prompt: {prompt_path}")
    benchmark_log(
        "GitHub publishing: SUCCESS"
        if publishing_result.get("status") == "published"
        else f"GitHub publishing: {publishing_result.get('status', 'unknown').upper()}"
    )
    benchmark_log(publishing_result["message"])
    if publishing_result.get("published"):
        benchmark_log("Published:")
        for path in publishing_result["published"]:
            benchmark_log(f"- {path}")
    benchmark_log("=" * 40)
    result["execution"] = {
        "run_id": run_id,
        "benchmark_status": evaluation["overall_status"],
        "result_path": output_path,
        "evaluation_path": evaluation_path,
        "fix_prompt_path": prompt_path,
        "github_published": publishing_result.get("status") == "published",
        "github_publishing": publishing_result
    }
    return result


def get_benchmark_case_ids():
    return [case["id"] for case in _load_cases()]


def run_all_benchmarks(run_id=None, progress_callback=None):
    results = []
    for case in _load_cases():
        results.append(run_case(case, run_id=run_id, progress_callback=progress_callback))
    return {
        "run_id": run_id,
        "results": results,
        "failed": any(bool(result.get("failure")) for result in results)
    }


def main():
    failed = False
    try:
        suite = run_all_benchmarks()
        failed = suite["failed"]
    except Exception as error:
        print(f"BENCHMARK FAILED: {error}", file=sys.stderr, flush=True)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
