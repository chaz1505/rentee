import json
import os
import tempfile

try:
    from .bubble_test_data import (
        BubbleTestDataError,
        bubble_delete,
        bubble_get,
        bubble_patch,
        bubble_post,
    )
except ImportError:
    from bubble_test_data import (
        BubbleTestDataError,
        bubble_delete,
        bubble_get,
        bubble_patch,
        bubble_post,
    )


STATE_PATH = os.path.join(os.path.dirname(__file__), ".autotest_state.json")


class LiveBenchmarkSafetyError(RuntimeError):
    pass


def _log(message):
    print(f"[BENCHMARK] {message}", flush=True)


def _require_live_enabled(environment):
    if (
        environment == "live"
        and os.environ.get("BENCHMARK_LIVE_ENABLED", "").strip().lower() != "true"
    ):
        raise LiveBenchmarkSafetyError(
            "Live benchmark is disabled; BENCHMARK_LIVE_ENABLED=true is required."
        )


def _load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as state_file:
            data = json.load(state_file)
            if not isinstance(data, dict):
                return {"development": {}, "live": {}}
            if "development" in data or "live" in data:
                return {
                    "development": data.get("development", {}),
                    "live": data.get("live", {})
                }
            # Migrate the original development-only state shape in memory.
            return {"development": data, "live": {}}
    except FileNotFoundError:
        return {"development": {}, "live": {}}
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read benchmark state {STATE_PATH}: {error}") from error


def _save_state(state):
    state_dir = os.path.dirname(STATE_PATH)
    os.makedirs(state_dir, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=state_dir, delete=False
        ) as temporary_file:
            temporary_path = temporary_file.name
            json.dump(state, temporary_file, indent=2)
            temporary_file.write("\n")
        os.replace(temporary_path, STATE_PATH)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _saved_subject_exists(subject, environment="development"):
    if not isinstance(subject, dict) or not subject.get("lead_id") or not subject.get("folio_id"):
        return False
    try:
        return bool(
            bubble_get(f"obj/lead/{subject['lead_id']}", environment)
            and bubble_get(f"obj/folio/{subject['folio_id']}", environment)
        )
    except BubbleTestDataError:
        return False


def verify_live_test_subject(case, subject):
    _log("Verifying dedicated LIVE test subject...")
    if not isinstance(subject, dict) or not subject.get("lead_id") or not subject.get("folio_id"):
        raise LiveBenchmarkSafetyError("Saved live benchmark subject IDs are missing.")
    try:
        lead = bubble_get(f"obj/lead/{subject['lead_id']}", "live")
    except Exception as error:
        raise LiveBenchmarkSafetyError("Saved live benchmark Lead does not exist.") from error
    if lead.get("test") is not True:
        raise LiveBenchmarkSafetyError(
            "Saved live benchmark Lead is not marked test=true."
        )
    _log("Lead.test = true")
    try:
        folio = bubble_get(f"obj/folio/{subject['folio_id']}", "live")
    except Exception as error:
        raise LiveBenchmarkSafetyError("Saved live benchmark Folio does not exist.") from error
    if folio.get("lead") != subject["lead_id"]:
        raise LiveBenchmarkSafetyError(
            "Saved live benchmark Folio is not linked to the verified test Lead."
        )
    _log("Folio is linked to verified test Lead.")
    _log("Live test subject verified.")
    return lead, folio


def _live_safety_failure(error):
    _log("LIVE SAFETY CHECK FAILED")
    _log(str(error))
    _log("No live data was modified.")


def ensure_test_subject(case, environment="development"):
    _require_live_enabled(environment)
    state = _load_state()
    environment_state = state.setdefault(environment, {})
    saved_subject = environment_state.get(case["id"])
    if environment == "live" and saved_subject:
        try:
            verify_live_test_subject(case, saved_subject)
        except LiveBenchmarkSafetyError as error:
            _live_safety_failure(error)
            raise
        return saved_subject
    if environment == "development" and _saved_subject_exists(saved_subject, environment):
        return saved_subject

    if environment == "live":
        _log("No live Sofia test subject exists.")
        _log("Creating dedicated live benchmark Lead with test=true...")

    try:
        lead_id = bubble_post("obj/lead", {
            **({"test": True} if environment == "live" else {}),
            "AIsearchtext": case["initial_ai_searchtext"],
            "AIsearchsummary": ""
        }, environment)
    except Exception as error:
        raise RuntimeError(f"Bubble {environment} Lead creation failed: {error}") from error

    if environment == "live":
        _log("Creating linked live benchmark Folio...")
    try:
        folio_id = bubble_post("obj/folio", {
            "lead": lead_id,
            "folioItems": [],
            "newRecommendations": False
        }, environment)
    except Exception as error:
        raise RuntimeError(f"Bubble {environment} Folio creation failed: {error}") from error

    subject = {"lead_id": lead_id, "folio_id": folio_id}
    environment_state[case["id"]] = subject
    _save_state(state)
    if environment == "live":
        _log("Dedicated live benchmark subject created.")
    return subject


def reset_test_subject(case, subject, environment="development"):
    _require_live_enabled(environment)
    try:
        if environment == "live":
            try:
                _lead, folio = verify_live_test_subject(case, subject)
            except LiveBenchmarkSafetyError as error:
                _live_safety_failure(error)
                raise
        else:
            folio = bubble_get(f"obj/folio/{subject['folio_id']}", environment)
        old_folio_items = list(folio.get("folioItems", []) or [])
        bubble_patch(f"obj/folio/{subject['folio_id']}", {
            "folioItems": [],
            "newRecommendations": False
        }, environment)
        for folio_item_id in old_folio_items:
            try:
                bubble_delete(f"obj/folioItem/{folio_item_id}", environment)
            except BubbleTestDataError as error:
                print(
                    f"WARNING: Could not delete old {environment} FolioItem "
                    f"{folio_item_id}: {error}",
                    flush=True
                )
        bubble_patch(f"obj/lead/{subject['lead_id']}", {
            "AIsearchtext": case["initial_ai_searchtext"],
            "AIsearchsummary": ""
        }, environment)
    except LiveBenchmarkSafetyError:
        raise
    except Exception as error:
        raise RuntimeError(f"Reset failed: {error}") from error


def snapshot_test_subject(subject, environment="development"):
    lead = bubble_get(f"obj/lead/{subject['lead_id']}", environment)
    folio = bubble_get(f"obj/folio/{subject['folio_id']}", environment)
    return {
        "lead": {
            "AIsearchtext": lead.get("AIsearchtext", ""),
            "AIsearchsummary": lead.get("AIsearchsummary", "")
        },
        "folio": {
            "folioItems": list(folio.get("folioItems", []) or []),
            "newRecommendations": folio.get("newRecommendations", False)
        }
    }
