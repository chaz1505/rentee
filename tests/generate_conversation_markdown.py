import json
import os
import re


def _display(value):
    return "unavailable" if value is None else str(value)


def _timing(value):
    return "unavailable" if value is None else f"{value}s"


def _completion_reason(result, case):
    if result.get("failure") or result.get("infrastructure_error"):
        return "error"
    if case.get("conversation_mode") == "synthetic":
        status = result.get("synthetic_completion", {}).get("status")
        if status == "success":
            return "synthetic success"
        if status == "max_turns":
            return "maximum turns"
        return status or "synthetic incomplete"
    return "scripted completion"


def _safe_run_id(run_id):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(run_id or "benchmark_run"))
    return safe.strip("._") or "benchmark_run"


def generate_conversation_markdown(result, case, results_dir):
    os.makedirs(results_dir, exist_ok=True)
    safe_run_id = _safe_run_id(result.get("run_id"))
    safe_case_id = _safe_run_id(result.get("case_id", case.get("id")))
    artifact_stem = safe_run_id
    if safe_case_id.casefold() not in safe_run_id.casefold():
        artifact_stem = f"{safe_run_id}_{safe_case_id}"
    path = os.path.join(
        results_dir,
        f"{artifact_stem}_conversation.md",
    )
    completion_reason = _completion_reason(result, case)
    synthetic = result.get("synthetic_completion", {})
    lines = [
        "# Rentee Benchmark Conversation",
        "",
        "## Run",
        "",
        f"Case: {result.get('case_name', case.get('name', ''))}",
        f"Case ID: {result.get('case_id', case.get('id', ''))}",
        f"Run ID: {result.get('run_id', '')}",
        f"Environment: {result.get('bubble_env', '')}",
        f"Completion reason: {completion_reason}",
        f"Customer turns: {len(result.get('turns', []))}",
    ]
    wanted = synthetic.get("wants_to_view") or []
    if wanted:
        lines.extend(["", "Wanted to view:"])
        lines.extend(f"- {listing}" for listing in wanted)

    lines.extend(["", "## Conversation", ""])
    for turn in result.get("turns", []):
        timing = turn.get("timing", {})
        lines.extend([
            f"### Turn {turn.get('turn', '')}",
            "",
            "CUSTOMER:",
            "",
            turn.get("tenant_message", ""),
            "",
            "RENTEE:",
            "",
            turn.get("rentee_response", ""),
            "",
            "Timing:",
            f"- First event: {_timing(timing.get('first_event_s'))}",
            f"- First text: {_timing(timing.get('first_delta_s'))}",
            f"- Total: {_timing(timing.get('total_s'))}",
        ])
        errors = turn.get("errors") or []
        if errors:
            lines.extend(["", "Errors:"])
            lines.extend(f"- {error}" for error in errors)
        lines.append("")

    ai_searchtext = (
        result.get("final_state", {}).get("lead", {}).get("AIsearchtext")
    )
    lines.extend([
        "## Final Customer State",
        "",
        "AIsearchtext:",
        "",
        ai_searchtext if ai_searchtext is not None else "unavailable",
        "",
        "## Benchmark Diagnostics",
        "",
    ])
    for turn in result.get("turns", []):
        lines.append(
            f"- Turn {turn.get('turn', '')}: response_id="
            f"{_display(turn.get('response_id'))}; previous_response_id_sent="
            f"{_display(turn.get('previous_response_id_sent'))}; "
            f"done_seen={turn.get('done_seen', False)}"
        )
    if result.get("failure"):
        lines.append(
            "- Failure: " + json.dumps(result["failure"], ensure_ascii=False)
        )
    if result.get("infrastructure_error"):
        lines.append(f"- Infrastructure error: {result['infrastructure_error']}")
    if not result.get("turns") and not result.get("failure"):
        lines.append("- No turn-level diagnostics were captured.")

    if case.get("conversation_mode") == "synthetic":
        lines.extend([
            "",
            "## Synthetic Customer Ground Truth",
            "",
            "Hidden persona and true requirements:",
            "",
            "```json",
            json.dumps(case.get("synthetic_persona", {}), indent=2, ensure_ascii=False),
            "```",
            "",
            f"Termination reason: {completion_reason}",
            f"Successful completion reached: {synthetic.get('status') == 'success'}",
            f"Maximum 15-customer-turn limit reached: {synthetic.get('status') == 'max_turns'}",
        ])
        if wanted:
            lines.extend(["", "Listings wanted to view:"])
            lines.extend(f"- {listing}" for listing in wanted)

    with open(path, "w", encoding="utf-8") as output:
        output.write("\n".join(lines).rstrip() + "\n")
    return path
