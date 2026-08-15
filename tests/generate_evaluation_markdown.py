import json
import os
import re
from datetime import datetime


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
CATEGORY_ORDER = {
    "infrastructure": 0, "capability": 1, "grounding": 1,
    "task_completion": 2, "state": 3, "speed": 4, "conversation": 5
}


def _load(path):
    with open(path, "r", encoding="utf-8") as source:
        return json.load(source)


def _label(value):
    return str(value or "").replace("_", " ").strip().title()


def _seconds(value):
    return "Unavailable" if value is None else f"{value:.1f}s"


def _speed_label(value):
    if value is None:
        return "Unavailable"
    if value <= 5:
        return "Good"
    if value <= 10:
        return "Acceptable"
    if value <= 20:
        return "Slow"
    return "Poor"


def _redact(text):
    text = str(text or "")
    for name in ("GITHUB_RESULTS_TOKEN", "BUBBLE_API_TOKEN", "OPENAI_API_KEY", "BENCHMARK_API_KEY"):
        secret = os.environ.get(name, "")
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)\b(?:sk-|github_pat_|ghp_)[A-Za-z0-9_-]{12,}\b",
        "[REDACTED]",
        text
    )
    return text


def _run_time(result):
    value = result.get("started_at_utc")
    if not value:
        return "Unknown"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return value


def _headline(result, evaluation, issues):
    if result.get("failure"):
        turn = result["failure"].get("turn")
        captured = len(result.get("turns", [])) - 1 if turn else len(result.get("turns", []))
        return (
            f"The benchmark stopped on Turn {turn or 'unknown'} because of an infrastructure "
            f"or conversation-chain failure. {max(captured, 0)} earlier turns were captured successfully."
        )
    if not issues:
        return "Rentee completed the benchmark without any issues detected by the configured evaluation checks."
    readable = [_label(issue.get("id")) for issue in issues[:2]]
    return f"Rentee completed the flow, but the evaluation detected {', '.join(readable).lower()}. {evaluation.get('summary', '')}".strip()


def _comparison_lines(comparison, metrics):
    if not comparison.get("available"):
        return ["No previous run available for comparison."]
    lines = []
    for metric_name, change_name, label in (
        ("average_first_delta_s", "first_delta_change_pct", "Average first text"),
        ("average_total_s", "total_latency_change_pct", "Average total time")
    ):
        current = metrics.get(metric_name)
        change = comparison.get(change_name)
        prior = None
        if current is not None and change is not None and change != -100:
            prior = current / (1 + change / 100)
        direction = "faster" if change is not None and change < 0 else "slower" if change and change > 0 else "unchanged"
        if prior is not None:
            lines.append(f"- {label}: **{prior:.1f}s → {current:.1f}s ({abs(change):.1f}% {direction})**")
    mappings = (
        ("slow_turns", "Slow turns"),
        ("unsupported_actions", "Unsupported actions"),
        ("excessive_questioning", "Excessive-question turns"),
        ("repeated_questions", "Repeated-question violations"),
        ("preference_persistence_failures", "Preference persistence failures")
    )
    for key, label in mappings:
        values = comparison.get(key)
        if isinstance(values, dict):
            previous, current = values.get("previous"), values.get("current")
            outcome = "improved" if current is not None and previous is not None and current < previous else "worsened" if current is not None and previous is not None and current > previous else "unchanged"
            lines.append(f"- {label}: **{previous} → {current} ({outcome})**")
    return lines or ["Previous-run data was available, but no comparable metrics were recorded."]


def generate_evaluation_markdown(result_path, evaluation_path):
    result = _load(result_path)
    evaluation = _load(evaluation_path)
    issues = sorted(
        evaluation.get("issues", []),
        key=lambda issue: (
            SEVERITY_ORDER.get(issue.get("severity"), 9),
            CATEGORY_ORDER.get(issue.get("category"), 9)
        )
    )
    result_label = evaluation.get("overall_status", "unknown").upper()
    if result.get("failure"):
        result_label += " — infrastructure failure"
    lines = [
        f"# Rentee Benchmark Evaluation — {result.get('case_name', result.get('case_id', 'Unknown'))}",
        "",
        f"**Result:** {result_label}  ",
        f"**Run ID:** {result.get('run_id', 'unavailable')}  ",
        f"**Run:** {_run_time(result)}  ",
        f"**Case:** {result.get('case_name', result.get('case_id', 'Unknown'))}",
        "",
        "## Headline",
        "",
        _headline(result, evaluation, issues),
        "",
        "## Speed",
        "",
        "| Turn | First text | Total | Assessment |",
        "|---|---:|---:|---|"
    ]
    for turn in result.get("turns", []):
        timing = turn.get("timing", {})
        lines.append(
            f"| {turn.get('turn')} | {_seconds(timing.get('first_delta_s'))} | "
            f"{_seconds(timing.get('total_s'))} | {_speed_label(timing.get('first_delta_s'))} |"
        )
    metrics = evaluation.get("metrics", {})
    lines.extend([
        "",
        f"**Average first text:** {_seconds(metrics.get('average_first_delta_s'))}  ",
        f"**Average total:** {_seconds(metrics.get('average_total_s'))}",
        "",
        "## Versus previous run",
        "",
        *_comparison_lines(evaluation.get("comparison_to_previous_run", {}), metrics),
        "",
        "## What worked",
        ""
    ])
    strengths = list(evaluation.get("passes", []))
    strengths.extend(evaluation.get("qualitative_evaluation", {}).get("strengths", []))
    lines.extend([f"- {strength}" for strength in strengths] or ["No explicit passes or strengths were recorded."])
    lines.extend(["", "## Problems found", ""])
    if not issues:
        lines.append("No problems were detected.")
    for index, issue in enumerate(issues, start=1):
        lines.extend([
            f"### {index}. {_label(issue.get('id'))} — {str(issue.get('severity', 'unknown')).upper()}",
            "",
            issue.get("diagnosis") or "The evaluation detected this issue.",
            "",
            "**Evidence**",
            ""
        ])
        lines.extend([f"- {_redact(item)}" for item in issue.get("evidence", [])[:3]] or ["- No additional evidence recorded."])
        if issue.get("recommended_fix"):
            lines.extend(["", "**Desired behaviour**", "", issue["recommended_fix"]])
        lines.append("")

    qualitative = evaluation.get("qualitative_evaluation", {})
    lines.extend(["## Qualitative scores", ""])
    if qualitative.get("error"):
        lines.append(f"Qualitative evaluation unavailable: {_redact(qualitative['error'])}")
    elif qualitative.get("scores"):
        lines.extend(["| Dimension | Score |", "|---|---:|"])
        for name, score in qualitative["scores"].items():
            lines.append(f"| {_label(name)} | {score}/3 |")
        if qualitative.get("highest_priority_improvement"):
            lines.extend(["", f"**Evaluator priority:** {qualitative['highest_priority_improvement']}"])
    else:
        lines.append("Qualitative evaluation unavailable.")

    lines.extend(["", "## Top priorities", ""])
    priorities = issues[:3]
    if priorities:
        for index, issue in enumerate(priorities, start=1):
            direction = issue.get("recommended_fix") or issue.get("diagnosis") or _label(issue.get("id"))
            lines.append(f"{index}. {direction}")
    else:
        lines.append("No improvement priorities were recorded.")

    turn_issues = {}
    for issue in issues:
        for turn_number in issue.get("turns", []):
            turn_issues.setdefault(turn_number, []).append(_label(issue.get("id")))
    lines.extend(["", "## Full conversation", ""])
    for turn in result.get("turns", []):
        number = turn.get("turn")
        lines.extend([f"### Turn {number}", ""])
        markers = turn_issues.get(number, [])
        if markers:
            lines.extend([f"**Issues detected:** {' · '.join(markers)}", ""])
        lines.extend(["**Tenant**", "", turn.get("tenant_message", ""), "", "**Rentee**", ""])
        if turn.get("errors") and not turn.get("rentee_response"):
            lines.extend(["⚠️ **Response failed**", "", f"`{_redact(' | '.join(map(str, turn['errors'])))}`"])
        else:
            lines.append(turn.get("rentee_response", ""))
            if turn.get("errors"):
                lines.extend(["", f"⚠️ **Error:** `{_redact(' | '.join(map(str, turn['errors'])))}`"])
        timing = turn.get("timing", {})
        lines.extend([
            "",
            f"**Timing:** First event {_seconds(timing.get('first_event_s'))} · "
            f"First text {_seconds(timing.get('first_delta_s'))} · Total {_seconds(timing.get('total_s'))}",
            "",
            "---",
            ""
        ])
    markdown = _redact("\n".join(lines).rstrip() + "\n")
    markdown_path = result_path[:-5] + "_evaluation.md"
    with open(markdown_path, "w", encoding="utf-8") as output:
        output.write(markdown)
    return markdown_path
