import json
import os
from datetime import datetime, timezone

import requests

try:
    from .bubble_test_data import get_bubble_base
except ImportError:
    from bubble_test_data import get_bubble_base


REQUEST_TIMEOUT = 30
BENCHMARK_RUN_FIELDS = {
    "runID", "caseID", "caseName", "environment", "status",
    "evaluationJSON", "evaluationMarkdown", "fixPrompt", "previousRunID",
    "rawResultJSON", "summary", "averageFirstText", "averageTotal",
    "criticalIssueCount", "maxFirstText", "questionQuality",
    "recommendationReasoning", "adaptiveness", "decisionProgress",
    "totalTurns", "conversationIntelligence", "startedAt", "completedAt"
}


class BenchmarkPersistenceError(RuntimeError):
    pass


def _headers():
    return {
        "Authorization": f"Bearer {os.environ['BUBBLE_API_TOKEN']}",
        "Content-Type": "application/json"
    }


def _response_body(response):
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _safe_error(response):
    body = _response_body(response)
    rendered = json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else str(body or "")
    return f"HTTP {response.status_code}: {rendered}"


def _iso_date(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def build_benchmark_run_payload(result, evaluation, evaluation_markdown, fix_prompt, environment):
    metrics = evaluation.get("metrics", {})
    first_text_values = [
        turn.get("timing", {}).get("first_delta_s")
        for turn in result.get("turns", [])
    ]
    first_text_values = [value for value in first_text_values if _number(value) is not None]
    issues = evaluation.get("issues", [])
    comparison = evaluation.get("comparison_to_previous_run", {})
    previous_run_id = comparison.get("previous_run_id")
    scores = evaluation.get("qualitative_evaluation", {}).get("scores", {})
    payload = {
        "runID": result.get("run_id"),
        "caseID": result.get("case_id"),
        "caseName": result.get("case_name"),
        "environment": environment,
        "status": "error" if result.get("infrastructure_error") else evaluation.get("overall_status", "fail"),
        "startedAt": _iso_date(result.get("started_at_utc")),
        "completedAt": _iso_date(result.get("completed_at_utc")),
        "criticalIssueCount": sum(
            1 for issue in issues if str(issue.get("severity", "")).lower() in ("critical", "high")
        ),
        "totalTurns": len(result.get("turns", [])),
        "rawResultJSON": json.dumps(result, ensure_ascii=False),
        "evaluationJSON": json.dumps(evaluation, ensure_ascii=False),
        "evaluationMarkdown": evaluation_markdown,
        "fixPrompt": fix_prompt,
        "summary": evaluation.get("summary")
    }
    optional = {
        "averageFirstText": _number(metrics.get("average_first_delta_s")),
        "averageTotal": _number(metrics.get("average_total_s")),
        "maxFirstText": max(first_text_values) if first_text_values else None,
        "previousRunID": previous_run_id,
        "conversationIntelligence": _number(scores.get("conversation_intelligence")),
        "recommendationReasoning": _number(scores.get("recommendation_reasoning")),
        "adaptiveness": _number(scores.get("adaptiveness")),
        "questionQuality": _number(scores.get("question_quality")),
        "decisionProgress": _number(scores.get("decision_progress"))
    }
    payload.update({key: value for key, value in optional.items() if value is not None and value != ""})
    return {key: value for key, value in payload.items() if key in BENCHMARK_RUN_FIELDS and value is not None}


def save_benchmark_run(result, evaluation, evaluation_markdown, fix_prompt, environment):
    url = f"{get_bubble_base(environment)}/obj/benchmarkRun"
    payload = build_benchmark_run_payload(
        result, evaluation, evaluation_markdown, fix_prompt, environment
    )
    response = requests.post(
        url, headers=_headers(), json=payload, timeout=REQUEST_TIMEOUT
    )
    if not response.ok:
        raise BenchmarkPersistenceError(_safe_error(response))
    body = _response_body(response)
    created_id = None
    if isinstance(body, dict):
        created_id = body.get("_id") or body.get("id")
        nested = body.get("response")
        if not created_id and isinstance(nested, dict):
            created_id = nested.get("_id") or nested.get("id")
    if not created_id:
        raise BenchmarkPersistenceError(
            "Bubble BenchmarkRun create succeeded but returned no object ID."
        )
    return created_id


def get_previous_benchmark_run(case_id, environment, current_run_id):
    url = f"{get_bubble_base(environment)}/obj/benchmarkRun"
    constraints = json.dumps([
        {"key": "caseID", "constraint_type": "equals", "value": case_id},
        {"key": "environment", "constraint_type": "equals", "value": environment},
        {"key": "runID", "constraint_type": "not equal", "value": current_run_id}
    ])
    response = requests.get(
        url,
        headers=_headers(),
        params={"constraints": constraints, "sort_field": "completedAt", "descending": "true", "limit": 1},
        timeout=REQUEST_TIMEOUT
    )
    if not response.ok:
        raise BenchmarkPersistenceError(_safe_error(response))
    body = _response_body(response)
    results = body.get("response", {}).get("results", []) if isinstance(body, dict) else []
    return results[0] if results else None
