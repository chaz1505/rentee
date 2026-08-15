import json
import os
import sys


PRIORITY = {
    "infrastructure": 0,
    "capability": 1,
    "grounding": 1,
    "task_completion": 2,
    "state": 3,
    "speed": 4,
    "conversation": 5
}
SEVERITY = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _load(path):
    with open(path, "r", encoding="utf-8") as source:
        return json.load(source)


def _format_metric(value):
    return "unavailable" if value is None else f"{value:.3f}s"


def generate_fix_prompt(result_path, evaluation_path):
    result_path = os.path.abspath(result_path)
    evaluation_path = os.path.abspath(evaluation_path)
    result = _load(result_path)
    evaluation = _load(evaluation_path)
    ranked = sorted(
        evaluation.get("issues", []),
        key=lambda issue: (
            SEVERITY.get(issue.get("severity"), 9),
            PRIORITY.get(issue.get("category"), 9)
        )
    )[:5]
    metrics = evaluation.get("metrics", {})
    qualitative = evaluation.get("qualitative_evaluation", {})

    lines = [
        "# Improve Rentee from the latest end-to-end benchmark",
        "",
        "Inspect the current repository before changing anything. Diagnose the relevant existing code paths and make general fixes supported by the evidence below. Do not special-case Sofia or optimise to exact benchmark wording.",
        "",
        "## Benchmark summary",
        "",
        f"- Case: {result.get('case_name')} (`{result.get('case_id')}`)",
        f"- Status: {evaluation.get('overall_status', 'unknown').upper()}",
        f"- Average first customer-facing text: {_format_metric(metrics.get('average_first_delta_s'))}",
        f"- Average completion time: {_format_metric(metrics.get('average_total_s'))}",
        f"- Slow turns (>10s): {metrics.get('slow_turns', [])}",
        f"- Critical slow turns (>20s): {metrics.get('critical_slow_turns', [])}",
        ""
    ]
    if qualitative.get("scores"):
        lines.extend([
            "Qualitative scores (0 poor, 3 excellent): " + ", ".join(
                f"{name}={score}" for name, score in qualitative["scores"].items()
            ),
            f"Highest-priority behavioural improvement: {qualitative.get('highest_priority_improvement', '')}",
            ""
        ])
        if qualitative.get("strengths"):
            lines.append("Strengths to preserve: " + "; ".join(qualitative["strengths"]))
            lines.append("")

    lines.extend(["## Highest-impact issues to fix", ""])
    if not ranked:
        lines.extend([
            "The benchmark found no deterministic failures. Keep the change narrow and inspect only the qualitative improvement noted above.",
            ""
        ])
    for position, issue in enumerate(ranked, start=1):
        lines.extend([
            f"### {position}. {issue['id'].replace('_', ' ').title()} ({issue['severity']})",
            "",
            f"Symptom: {issue['diagnosis']}",
            "",
            "Evidence:"
        ])
        lines.extend(f"- {evidence}" for evidence in issue.get("evidence", [])[:6])
        lines.extend([
            "",
            f"Desired behaviour: {issue['recommended_fix']}",
            ""
        ])
        if issue["id"] == "preference_update_latency":
            lines.extend([
                "Inspect the `update_preferences` continuation and `stream_match_lead` flow in `app.py`. The benchmark evidence is consistent with a full rematch running after a simple preference-only update, but confirm the exact cause before changing it.",
                ""
            ])
        elif issue["id"] in {"unsupported_actions", "excessive_questioning", "repeated_questions"}:
            lines.extend([
                "Inspect the main AI instructions and relevant tool-result continuation instructions in `app.py`; preserve tool routing while tightening the behavioural instruction.",
                ""
            ])
        elif issue["id"] in {"recommendation_request_not_answered", "historical_recommendation_reliance"}:
            lines.extend([
                "Inspect recommendation-request routing and the current `match_lead` result continuation in `app.py`. Current recommendations must be presented from the current match result, not recalled from history.",
                ""
            ])
        elif issue["id"] == "preference_persistence":
            lines.extend([
                "Inspect preference merging and the Bubble Lead PATCH in `update_preferences`; retain all earlier requirements while applying the newest one.",
                ""
            ])

    lines.extend([
        "## Regression constraints",
        "",
        "Preserve all of the following:",
        "",
        "- Bubble development/live isolation.",
        "- `previous_response_id` conversation continuity.",
        "- Lead preference persistence.",
        "- Current-listing grounding and the rule that current recommendations come from a current `match_lead` result.",
        "- Condo-data grounding and unit-detail grounding.",
        "- Folio/FolioItem creation and valid Bubble IDs.",
        "- The prohibition on invented listing IDs.",
        "- Existing web-search safeguards.",
        "- Preference-only turns must still persist successfully even if unnecessary rematching is removed.",
        "",
        "Do not remove safety or grounding behaviour merely to reduce latency. Do not modify the benchmark to make the result pass.",
        "",
        "## Verification",
        "",
        "Add or update focused regression tests, run the automated test suite, then rerun:",
        "",
        "```bash",
        "python tests/run_benchmark.py",
        "```",
        "",
        "Report the exact code changes, test results, and before/after benchmark metrics for average first-text latency, average total latency, slow turns, unsupported-action violations, excessive/repeated questions, preference-persistence failures, and qualitative scores."
    ])
    prompt_path = result_path[:-5] + "_fix_prompt.md"
    with open(prompt_path, "w", encoding="utf-8") as output:
        output.write("\n".join(lines).rstrip() + "\n")
    return prompt_path


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python tests/generate_fix_prompt.py <result.json> <evaluation.json>")
    print(generate_fix_prompt(sys.argv[1], sys.argv[2]))
