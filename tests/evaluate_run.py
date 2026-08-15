import glob
import json
import os
import re
import statistics
import sys

from openai import OpenAI


TESTS_DIR = os.path.dirname(__file__)
RESULTS_DIR = os.path.join(TESTS_DIR, "results")
CASE_PATH = os.path.join(TESTS_DIR, "benchmark_cases.json")
EVALUATOR_MODEL = os.environ.get("RENTEE_EVALUATOR_MODEL", "gpt-5-mini")

UNSUPPORTED_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"\b(?:I|we)(?:'ll| will| can)\s+(?:contact|call|message|email|ask|check with|confirm with|reach out to)\s+(?:the\s+|your\s+)?(?:agent|agents|owner|owners|property management|management|listings?|shortlisted listings?)",
        r"\b(?:contact|call|message|email|ask)\s+(?:the\s+)?(?:agent|agents|owner|owners|property management)\s+(?:for you|on your behalf)",
        r"\b(?:I|we)(?:'ll| will| can)\s+(?:arrange|schedule|book|set up)\s+(?:a\s+|the\s+)?viewing",
        r"\b(?:arrange|schedule|book|set up)\s+(?:viewings?|a viewing)\s+(?:for you|on your behalf)",
        r"\b(?:I|we)(?:'ll| will| can)\s+(?:send|get|obtain|request)\s+(?:you\s+)?(?:photos?|floor ?plans?)",
        r"\b(?:I|we)(?:'ll| will| can)\s+(?:check|calculate|confirm)\s+(?:the\s+)?exact commute"
    )
]
HISTORY_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"\bI (?:already|previously) (?:identified|found|showed|recommended)",
        r"\b(?:the )?propert(?:y|ies) I showed earlier",
        r"\b(?:your|the) previous shortlist",
        r"\b(?:earlier|previous) recommendations?"
    )
]


def _load_json(path):
    with open(path, "r", encoding="utf-8") as source:
        return json.load(source)


def _load_case(case_id):
    cases = _load_json(CASE_PATH)
    try:
        return next(case for case in cases if case["id"] == case_id)
    except StopIteration as error:
        raise ValueError(f"No benchmark case definition found for {case_id}") from error


def _round_average(values):
    usable = [value for value in values if isinstance(value, (int, float))]
    return round(statistics.mean(usable), 3) if usable else None


def _excerpt(text, match, radius=90):
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return " ".join(text[start:end].split())


def _questions(text):
    found = []
    for match in re.finditer(r"[^?\n]{3,}\?", text or "", re.M):
        question = re.split(r"[.!]", match.group(0))[-1]
        question = " ".join(question.split())
        if question and question not in found:
            found.append(question)
    for line in (text or "").splitlines():
        cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        if re.match(r"^(?:what|which|when|where|who|why|how|do|does|did|is|are|can|could|would|will|have|has)\b", cleaned, re.I):
            if cleaned not in found:
                found.append(cleaned)
    return found


def _question_tokens(question):
    stop = {"a", "an", "the", "you", "your", "is", "are", "do", "does", "would", "could", "please", "still"}
    return {
        token for token in re.findall(r"[a-z0-9]+", question.lower())
        if len(token) > 1 and token not in stop
    }


def _similar_question(left, right):
    left_tokens, right_tokens = _question_tokens(left), _question_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens) >= 0.55


def _simple_preference_turn(message, expectation):
    words = re.findall(r"\b\w+\b", message or "")
    recommendation_language = re.search(
        r"\b(?:recommend|show|find|match|shortlist|available|options?)\b",
        message or "",
        re.I
    )
    return expectation != "recommendations" and len(words) <= 14 and not recommendation_language


def _issue(issue_id, category, severity, turns, evidence, diagnosis, recommended_fix):
    return {
        "id": issue_id,
        "category": category,
        "severity": severity,
        "turns": sorted(set(turns)),
        "evidence": evidence,
        "diagnosis": diagnosis,
        "recommended_fix": recommended_fix
    }


def deterministic_evaluation(result, case):
    issues, passes = [], []
    turns = result.get("turns", [])
    first_deltas = [turn.get("timing", {}).get("first_delta_s") for turn in turns]
    totals = [turn.get("timing", {}).get("total_s") for turn in turns]
    slow_turns = [turn["turn"] for turn in turns if (turn.get("timing", {}).get("first_delta_s") or 0) > 10]
    critical_turns = [turn["turn"] for turn in turns if (turn.get("timing", {}).get("first_delta_s") or 0) > 20]
    if slow_turns:
        severity = "critical" if critical_turns else "medium"
        evidence = [
            f"Turn {turn['turn']} first text: {turn['timing']['first_delta_s']:.3f}s"
            for turn in turns if turn["turn"] in slow_turns
        ]
        issues.append(_issue(
            "first_text_latency", "speed", severity, slow_turns, evidence,
            "Customer-facing text is delayed beyond the benchmark threshold.",
            "Trace the tool path and model calls before the first delta; preserve grounding while removing unnecessary work from non-recommendation turns."
        ))
    else:
        passes.append("Every turn produced customer-facing text within 10 seconds.")

    recommendation_totals = [
        turn.get("timing", {}).get("total_s") for turn, definition in zip(turns, case.get("turns", []))
        if definition.get("expectation") == "recommendations" and isinstance(turn.get("timing", {}).get("total_s"), (int, float))
    ]
    recommendation_average = _round_average(recommendation_totals)
    comparable_simple = []
    if recommendation_average:
        for turn, definition in zip(turns, case.get("turns", [])):
            total = turn.get("timing", {}).get("total_s")
            if _simple_preference_turn(turn.get("tenant_message", ""), definition.get("expectation")) and isinstance(total, (int, float)) and total >= recommendation_average * 0.8:
                comparable_simple.append(turn)
    if comparable_simple:
        issues.append(_issue(
            "preference_update_latency", "speed", "critical",
            [turn["turn"] for turn in comparable_simple],
            [f"Simple preference turn {turn['turn']} total: {turn['timing']['total_s']:.3f}s; recommendation-turn average: {recommendation_average:.3f}s" for turn in comparable_simple],
            "The simple update appears to execute approximately as much work as a recommendation turn. A likely cause, based on Rentee's architecture, is automatic rematching after update_preferences even when recommendations were not requested.",
            "Inspect the update_preferences continuation in app.py. Persist and briefly confirm preference-only updates without a full rematch; retain matching when the user actually asks for current recommendations."
        ))

    capability_evidence, capability_turns = [], []
    history_evidence, history_turns = [], []
    excessive, severe_questions = [], []
    all_questions = []
    for turn in turns:
        text = turn.get("rentee_response", "")
        for pattern in UNSUPPORTED_PATTERNS:
            for match in pattern.finditer(text):
                capability_turns.append(turn["turn"])
                capability_evidence.append(f"Turn {turn['turn']}: “{_excerpt(text, match)}”")
        questions = _questions(text)
        all_questions.append((turn["turn"], questions))
        if len(questions) > 3:
            excessive.append((turn["turn"], questions))
        if len(questions) > 5:
            severe_questions.append(turn["turn"])
        definition = case.get("turns", [])[turn["turn"] - 1]
        if definition.get("expectation") == "recommendations":
            for pattern in HISTORY_PATTERNS:
                for match in pattern.finditer(text):
                    history_turns.append(turn["turn"])
                    history_evidence.append(f"Turn {turn['turn']}: “{_excerpt(text, match)}”")
    if capability_evidence:
        issues.append(_issue(
            "unsupported_actions", "capability", "high", capability_turns,
            capability_evidence, "The assistant promises actions Rentee cannot perform.",
            "State the system's actual limits and give the user actionable next steps without promising agent contact, private checks, viewings, photos, or floorplans."
        ))
    else:
        passes.append("No unsupported action promises were detected.")
    if excessive:
        issues.append(_issue(
            "excessive_questioning", "conversation", "high" if severe_questions else "medium",
            [turn for turn, _ in excessive],
            [f"Turn {turn}: {len(questions)} questions — " + " | ".join(questions) for turn, questions in excessive],
            "The assistant asks too many clarification questions in one response.",
            "Ask at most one high-value clarification at a time unless several answers are truly required to proceed."
        ))

    repeats = []
    for index, (turn_number, questions) in enumerate(all_questions):
        for question in questions:
            for earlier_turn, earlier_questions in all_questions[:index]:
                for earlier_question in earlier_questions:
                    if _similar_question(question, earlier_question):
                        repeats.append((earlier_turn, earlier_question, turn_number, question))
    if repeats:
        issues.append(_issue(
            "repeated_questions", "conversation", "medium",
            [item for repeat in repeats for item in (repeat[0], repeat[2])],
            [f"Turns {a}/{b}: “{qa}” repeated as “{qb}”" for a, qa, b, qb in repeats],
            "Substantially similar clarification questions recur across turns.",
            "Use conversation history and persisted preferences before asking; do not repeat a resolved question without explaining why clarification is necessary."
        ))

    if history_evidence:
        issues.append(_issue(
            "historical_recommendation_reliance", "grounding", "high", history_turns,
            history_evidence, "A current recommendation request is answered by referring to historical recommendations rather than clearly grounding it in a current match_lead result.",
            "For every current recommendation request, use the current match_lead result and present the current shortlist directly."
        ))

    for turn, definition in zip(turns, case.get("turns", [])):
        if definition.get("expectation") != "recommendations":
            continue
        text = turn.get("rentee_response", "")
        concrete_signals = len(re.findall(r"(?:\bRM\s?[\d,]+|\b\d+\s*(?:bed|BR)\b|\b(?:condo|unit|listing)\b)", text, re.I))
        future_signals = len(re.findall(r"\b(?:I(?:'ll| will)|we(?:'ll| will)|next I|can then|will check|will contact)\b", text, re.I))
        if concrete_signals < 2 or future_signals > concrete_signals:
            issues.append(_issue(
                "recommendation_request_not_answered", "task_completion", "high", [turn["turn"]],
                [f"Turn {turn['turn']} had {concrete_signals} concrete recommendation signals and {future_signals} future-action signals: “{' '.join(text.split())[:500]}”"],
                "The explicit recommendation request does not appear to receive a concrete current shortlist.",
                "When recommendations are requested, call match_lead and lead with specific current options and relevant trade-offs before asking another question."
            ))

    final_text = result.get("final_state", {}).get("lead", {}).get("AIsearchtext", "") or ""
    missing_preferences = []
    for check in case.get("preference_checks", []):
        if not any(re.search(pattern, final_text, re.I) for pattern in check.get("patterns", [])):
            missing_preferences.append(check)
    if missing_preferences:
        issues.append(_issue(
            "preference_persistence", "state", "high", [],
            [f"Final AIsearchtext is missing: {check['description']}" for check in missing_preferences],
            "One or more explicitly supplied preferences are absent from the final Bubble Lead profile.",
            "Ensure update_preferences merges every new constraint into the complete AIsearchtext without dropping earlier requirements."
        ))
    else:
        passes.append("All configured preference checks were found in final AIsearchtext.")

    infra_evidence = []
    prior_response = None
    for turn in turns:
        if turn.get("errors"):
            infra_evidence.append(f"Turn {turn['turn']} errors: {turn['errors']}")
        if not turn.get("done_seen"):
            infra_evidence.append(f"Turn {turn['turn']} did not receive done=true")
        if not turn.get("response_id"):
            infra_evidence.append(f"Turn {turn['turn']} has no response_id")
        expected_previous = prior_response
        if "previous_response_id_sent" in turn and turn.get("previous_response_id_sent") != expected_previous:
            infra_evidence.append(
                f"Turn {turn['turn']} continuity mismatch: sent "
                f"{turn.get('previous_response_id_sent')!r}, expected {expected_previous!r}"
            )
        prior_response = turn.get("response_id")
    if not result.get("initial_state") or not result.get("final_state"):
        infra_evidence.append("Initial or final Bubble state snapshot is missing")
    if result.get("failure"):
        infra_evidence.append(
            f"Benchmark failure at turn {result['failure'].get('turn')}: "
            f"{result['failure'].get('message')}"
        )
    if len(turns) != len(case.get("turns", [])):
        infra_evidence.append(f"Expected {len(case.get('turns', []))} turns but captured {len(turns)}")
    if infra_evidence:
        issues.append(_issue(
            "infrastructure_failure", "infrastructure", "critical", [], infra_evidence,
            "The benchmark stream, continuity evidence, or Bubble snapshots are incomplete.",
            "Repair benchmark/chat infrastructure before interpreting conversation quality."
        ))

    metrics = {
        "total_turns": len(turns),
        "average_first_event_s": _round_average([turn.get("timing", {}).get("first_event_s") for turn in turns]),
        "average_first_delta_s": _round_average(first_deltas),
        "average_total_s": _round_average(totals),
        "slow_turns": slow_turns,
        "critical_slow_turns": critical_turns,
        "unsupported_action_violations": len(capability_evidence),
        "excessive_question_turns": len(excessive),
        "repeated_question_pairs": len(repeats),
        "preference_persistence_failures": len(missing_preferences)
    }
    return metrics, issues, passes


QUALITATIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "properties": {name: {"type": "integer", "minimum": 0, "maximum": 3} for name in (
                "conversation_intelligence", "recommendation_reasoning", "adaptiveness", "question_quality", "decision_progress"
            )},
            "required": ["conversation_intelligence", "recommendation_reasoning", "adaptiveness", "question_quality", "decision_progress"],
            "additionalProperties": False
        },
        "qualitative_issues": {"type": "array", "items": {"type": "string"}},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "highest_priority_improvement": {"type": "string"}
    },
    "required": ["scores", "qualitative_issues", "strengths", "highest_priority_improvement"],
    "additionalProperties": False
}


def qualitative_evaluation(case, result, deterministic_issues):
    transcript = [
        {"turn": turn["turn"], "tenant": turn["tenant_message"], "assistant": turn["rentee_response"]}
        for turn in result.get("turns", [])
    ]
    payload = {
        "case": {"name": case["name"], "turns": case["turns"], "hard_constraints": case.get("hard_constraints", {})},
        "transcript": transcript,
        "deterministic_findings": deterministic_issues,
        "final_AIsearchtext": result.get("final_state", {}).get("lead", {}).get("AIsearchtext", "")
    }
    instructions = (
        "Judge Rentee's tenant conversation behaviour, not its implementation. Score each requested dimension 0 poor, 1 weak, 2 good, 3 excellent. Assess whether questions were useful, prior supplied information was respected, adaptation and trade-off reasoning were sensible, and the final answer moved the tenant toward a shortlist. Identify asking for already-supplied facts where applicable. Do not invent implementation details or repeat deterministic findings unless qualitative context materially adds value. Return only the required JSON."
    )
    response = OpenAI(api_key=os.environ["OPENAI_API_KEY"]).responses.create(
        model=EVALUATOR_MODEL,
        instructions=instructions,
        input=json.dumps(payload, ensure_ascii=False),
        text={"format": {"type": "json_schema", "name": "benchmark_evaluation", "strict": True, "schema": QUALITATIVE_SCHEMA}}
    )
    return json.loads(response.output_text)


def _previous_result_path(result_path, case_id, environment="development"):
    candidates = []
    for path in glob.glob(os.path.join(RESULTS_DIR, f"{case_id}_*.json")):
        name = os.path.basename(path)
        if path == result_path or name.endswith("_evaluation.json"):
            continue
        try:
            candidate = _load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if candidate.get("bubble_env", "development") == environment:
            candidates.append(path)
    return max(candidates, key=os.path.getmtime) if candidates else None


def _count_issue(issues, issue_id):
    issue = next((item for item in issues if item["id"] == issue_id), None)
    return len(issue.get("evidence", [])) if issue else 0


def compare_previous(result_path, case, current_metrics, current_issues, environment="development", previous_benchmark_run=None):
    previous_path = _previous_result_path(
        result_path, case["id"], environment
    )
    previous_run_id = None
    previous_evaluation = None
    if previous_path:
        previous = _load_json(previous_path)
        previous_run_id = previous.get("run_id")
    elif previous_benchmark_run:
        try:
            previous = json.loads(previous_benchmark_run.get("rawResultJSON", ""))
            previous_evaluation = json.loads(previous_benchmark_run.get("evaluationJSON", ""))
            previous_run_id = previous_benchmark_run.get("runID")
        except (TypeError, json.JSONDecodeError):
            return {"available": False}
    else:
        return {"available": False}
    previous_metrics, previous_issues, _ = deterministic_evaluation(previous, case)
    def percentage(current, prior):
        return round((current - prior) / prior * 100, 1) if isinstance(current, (int, float)) and prior else None
    comparison = {
        "available": True,
        "previous_run_id": previous_run_id,
        "first_delta_change_pct": percentage(current_metrics["average_first_delta_s"], previous_metrics["average_first_delta_s"]),
        "total_latency_change_pct": percentage(current_metrics["average_total_s"], previous_metrics["average_total_s"]),
        "slow_turns": {"previous": len(previous_metrics["slow_turns"]), "current": len(current_metrics["slow_turns"])},
        "unsupported_actions": {"previous": _count_issue(previous_issues, "unsupported_actions"), "current": _count_issue(current_issues, "unsupported_actions")},
        "excessive_questioning": {"previous": _count_issue(previous_issues, "excessive_questioning"), "current": _count_issue(current_issues, "excessive_questioning")},
        "repeated_questions": {"previous": _count_issue(previous_issues, "repeated_questions"), "current": _count_issue(current_issues, "repeated_questions")},
        "preference_persistence_failures": {"previous": previous_metrics["preference_persistence_failures"], "current": current_metrics["preference_persistence_failures"]}
    }
    previous_evaluation_path = previous_path[:-5] + "_evaluation.json" if previous_path else None
    if previous_evaluation is None and previous_evaluation_path and os.path.exists(previous_evaluation_path):
        previous_evaluation = _load_json(previous_evaluation_path)
    if previous_evaluation:
        comparison["qualitative_scores"] = {
            "previous": previous_evaluation.get("qualitative_evaluation", {}).get("scores", {}),
            "current": {}
        }
    return comparison


def evaluate_run(result_path, run_qualitative=True, previous_benchmark_run=None):
    result_path = os.path.abspath(result_path)
    result = _load_json(result_path)
    case = _load_case(result["case_id"])
    metrics, issues, passes = deterministic_evaluation(result, case)
    qualitative = {}
    if run_qualitative:
        try:
            qualitative = qualitative_evaluation(case, result, issues)
        except Exception as error:
            qualitative = {"error": f"Qualitative evaluation failed: {error}"}
            issues.append(_issue(
                "qualitative_evaluator_failure", "infrastructure", "high", [],
                [qualitative["error"]],
                "The deterministic evaluation completed, but its qualitative model call failed.",
                "Fix evaluator credentials or model access without discarding the raw benchmark diagnostics."
            ))
    weak_scores = {
        name: score for name, score in qualitative.get("scores", {}).items()
        if score <= 1
    }
    if weak_scores or qualitative.get("qualitative_issues"):
        evidence = [f"{name}: {score}/3" for name, score in weak_scores.items()]
        evidence.extend(qualitative.get("qualitative_issues", []))
        issues.append(_issue(
            "qualitative_behavior", "conversation",
            "high" if any(score == 0 for score in weak_scores.values()) else "medium",
            [], evidence,
            "The qualitative judge found weak conversation intelligence, adaptation, question quality, recommendation reasoning, or decision progress.",
            qualitative.get("highest_priority_improvement") or "Address the specific behavioural evidence while preserving tool grounding."
        ))
    comparison = compare_previous(
        result_path, case, metrics, issues,
        result.get("bubble_env", "development"), previous_benchmark_run
    )
    if comparison.get("qualitative_scores") is not None:
        comparison["qualitative_scores"]["current"] = qualitative.get("scores", {})
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    issues.sort(key=lambda issue: severity_order.get(issue["severity"], 9))
    status = "fail" if issues else "pass"
    summary = f"{len(issues)} issue groups detected; {sum(1 for issue in issues if issue['severity'] in ('critical', 'high'))} critical/high."
    evaluation = {
        "run_id": result.get("run_id"),
        "case_id": case["id"],
        "overall_status": status,
        "summary": summary,
        "metrics": metrics,
        "issues": issues,
        "passes": passes,
        "qualitative_evaluation": qualitative,
        "comparison_to_previous_run": comparison
    }
    evaluation_path = result_path[:-5] + "_evaluation.json"
    with open(evaluation_path, "w", encoding="utf-8") as output:
        json.dump(evaluation, output, indent=2, ensure_ascii=False)
        output.write("\n")
    return evaluation_path, evaluation


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tests/evaluate_run.py tests/results/<result>.json")
    path, _ = evaluate_run(sys.argv[1])
    print(path)
