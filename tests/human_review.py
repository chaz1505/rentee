import json
import os
import re

import requests

try:
    from .bubble_test_data import get_bubble_base
except ImportError:
    from bubble_test_data import get_bubble_base


REQUEST_TIMEOUT = 30
HUMAN_REVIEW_START = "<!-- HUMAN_REVIEW_START -->"
HUMAN_REVIEW_END = "<!-- HUMAN_REVIEW_END -->"
HUMAN_SCORE_FIELDS = (
    ("humanScore", "Overall"),
    ("humanConversationScore", "Conversation quality"),
    ("humanRecommendationScore", "Recommendation quality"),
    ("humanAccuracyScore", "Accuracy / agent judgement"),
)
HUMAN_REVIEW_FIELDS = {
    "humanReviewed", "humanScore", "humanConversationScore",
    "humanRecommendationScore", "humanAccuracyScore", "humanFeedback",
    "humanInstruction", "humanReviewedAt",
}


class BenchmarkReviewError(RuntimeError):
    status_code = 502


class BenchmarkRunNotFound(BenchmarkReviewError):
    status_code = 404


class BenchmarkReviewValidationError(BenchmarkReviewError):
    status_code = 400


def _headers():
    return {
        "Authorization": f"Bearer {os.environ['BUBBLE_API_TOKEN']}",
        "Content-Type": "application/json",
    }


def _response_body(response):
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _safe_bubble_error(action, response):
    body = _response_body(response)
    rendered = (
        json.dumps(body, ensure_ascii=False)
        if isinstance(body, (dict, list))
        else str(body or "")
    )
    for name in ("BUBBLE_API_TOKEN", "BENCHMARK_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            rendered = rendered.replace(secret, "[REDACTED]")
    return BenchmarkReviewError(
        f"Bubble BenchmarkRun {action} failed: HTTP {response.status_code}: {rendered}"
    )


def _text(value):
    return str(value or "").strip()


def _score_present(value):
    return value is not None and value != ""


def build_human_review_block(record):
    lines = [
        HUMAN_REVIEW_START,
        "# Human agent review",
        "",
        "The following review was supplied after examining the benchmark conversation.",
    ]
    scores = [
        (label, record.get(field))
        for field, label in HUMAN_SCORE_FIELDS
        if _score_present(record.get(field))
    ]
    if scores:
        lines.extend(["", "## Scores", ""])
        lines.extend(f"- {label}: {score}/5" for label, score in scores)
    feedback = _text(record.get("humanFeedback"))
    if feedback:
        lines.extend(["", "## What was wrong with this run", "", feedback])
    instruction = _text(record.get("humanInstruction"))
    if instruction:
        lines.extend([
            "", "## How Rentee should behave differently", "", instruction
        ])
    lines.extend([
        "",
        "## How to use this feedback",
        "",
        "Treat this human agent review as high-value product-quality evidence.",
        "",
        "Do not blindly special-case this benchmark, Sofia, or the exact reviewer wording.",
        "",
        "Determine whether the feedback exposes a general weakness in Rentee's "
        "conversation flow, recommendation readiness, recommendation reasoning, "
        "grounding, or product logic.",
        "",
        "Implement a general solution where appropriate.",
        "",
        "Preserve all existing benchmark and live-safety constraints.",
        HUMAN_REVIEW_END,
    ])
    return "\n".join(lines)


def merge_human_review(fix_prompt, review_block):
    pattern = re.compile(
        rf"{re.escape(HUMAN_REVIEW_START)}.*?{re.escape(HUMAN_REVIEW_END)}",
        re.DOTALL,
    )
    if pattern.search(fix_prompt):
        return pattern.sub(lambda _match: review_block, fix_prompt, count=1)
    return f"{fix_prompt.rstrip()}\n\n{review_block}\n"


def update_fix_prompt_with_human_review(benchmark_run_id, environment):
    base_url = get_bubble_base(environment)
    url = f"{base_url}/obj/benchmarkRun/{benchmark_run_id}"
    response = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
    if response.status_code == 404:
        raise BenchmarkRunNotFound("BenchmarkRun not found.")
    if not response.ok:
        raise _safe_bubble_error("read", response)
    body = _response_body(response)
    record = body.get("response") if isinstance(body, dict) and "response" in body else body
    if not isinstance(record, dict) or not record:
        raise BenchmarkRunNotFound("BenchmarkRun not found.")

    review = {field: record.get(field) for field in HUMAN_REVIEW_FIELDS}

    if record.get("environment") != environment:
        raise BenchmarkReviewValidationError(
            "BenchmarkRun environment does not match the requested environment."
        )
    if review["humanReviewed"] is not True:
        raise BenchmarkReviewValidationError(
            "BenchmarkRun has not been human reviewed."
        )
    has_score = any(_score_present(review.get(field)) for field, _ in HUMAN_SCORE_FIELDS)
    if not (_text(review["humanFeedback"]) or _text(review["humanInstruction"]) or has_score):
        raise BenchmarkReviewValidationError(
            "BenchmarkRun contains no human review feedback."
        )
    fix_prompt = _text(record.get("fixPrompt"))
    if not fix_prompt:
        raise BenchmarkReviewValidationError(
            "BenchmarkRun has no generated fix prompt."
        )

    updated_prompt = merge_human_review(
        fix_prompt, build_human_review_block(review)
    )
    patch_response = requests.patch(
        url,
        headers=_headers(),
        json={"fixPrompt": updated_prompt},
        timeout=REQUEST_TIMEOUT,
    )
    if not patch_response.ok:
        raise _safe_bubble_error("update", patch_response)
    return {
        "benchmark_run_id": benchmark_run_id,
        "run_id": record.get("runID"),
        "environment": environment,
        "fix_prompt_updated": True,
    }
