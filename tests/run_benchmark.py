import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

try:
    from .bubble_test_data import get_bubble_dev_base
    from .test_subject import (
        ensure_test_subject,
        reset_test_subject,
        snapshot_test_subject,
    )
except ImportError:
    from bubble_test_data import get_bubble_dev_base
    from test_subject import ensure_test_subject, reset_test_subject, snapshot_test_subject


DEFAULT_STREAM_URL = "https://rentee-2.onrender.com/chat_stream"
STREAM_TIMEOUT = (30, 300)
TESTS_DIR = os.path.dirname(__file__)


class BenchmarkError(RuntimeError):
    pass


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
        raise BenchmarkError(f"SSE returned an error event: {result['errors']}")
    if not result["done_seen"]:
        raise BenchmarkError("SSE stream ended without done=true")
    if not result["response_id"]:
        raise BenchmarkError("No response_id returned; conversation continuity cannot be verified")
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


def run_case(case):
    # Force development-base validation before any Bubble request or chat turn.
    get_bubble_dev_base()
    print(f"\n=== {case['name']} ===\n", flush=True)
    subject = ensure_test_subject(case)
    reset_test_subject(case, subject)
    print("Reset complete", flush=True)
    print(f"Using Lead: {subject['lead_id']}", flush=True)
    print(f"Using Folio: {subject['folio_id']}", flush=True)

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
        "completed_at_utc": None
    }
    previous_response_id = None
    for turn_number, scripted_turn in enumerate(case["turns"], start=1):
        message = scripted_turn["message"]
        print(f"\nTURN {turn_number}\nTenant: {message}", flush=True)
        turn_result = run_turn(message, subject["folio_id"], previous_response_id)
        previous_response_id = turn_result["response_id"]
        timing = turn_result["timing"]
        print(f"First event: {timing['first_event_s']}s", flush=True)
        print(f"First text: {timing['first_delta_s']}s", flush=True)
        print(f"Total: {timing['total_s']}s", flush=True)
        print(f"\nRentee:\n{turn_result['text']}", flush=True)
        result["turns"].append({
            "turn": turn_number,
            "tenant_message": message,
            "rentee_response": turn_result["text"],
            "response_id": turn_result["response_id"],
            "statuses": turn_result["statuses"],
            "citations": turn_result["citations"],
            "errors": turn_result["errors"],
            "timing": turn_result["timing"],
            "done_seen": turn_result["done_seen"]
        })

    result["final_state"] = snapshot_test_subject(subject)
    result["completed_at_utc"] = _utc_now()
    output_path = _save_result(case["id"], result)
    print(f"\nSaved benchmark result: {output_path}", flush=True)
    return result


def main():
    try:
        for case in _load_cases():
            run_case(case)
    except Exception as error:
        print(f"BENCHMARK FAILED: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
