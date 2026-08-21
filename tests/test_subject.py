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


def _load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as state_file:
            data = json.load(state_file)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
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


def _saved_subject_exists(subject):
    if not isinstance(subject, dict) or not subject.get("lead_id") or not subject.get("folio_id"):
        return False
    try:
        return bool(
            bubble_get(f"obj/lead/{subject['lead_id']}")
            and bubble_get(f"obj/folio/{subject['folio_id']}")
        )
    except BubbleTestDataError:
        return False


def ensure_test_subject(case):
    state = _load_state()
    saved_subject = state.get(case["id"])
    if _saved_subject_exists(saved_subject):
        return saved_subject

    try:
        lead_id = bubble_post("obj/lead", {
            "AIsearchtext": case["initial_ai_searchtext"],
            "AIsearchsummary": ""
        })
    except Exception as error:
        raise RuntimeError(f"Bubble development Lead creation failed: {error}") from error

    try:
        folio_id = bubble_post("obj/folio", {
            "lead": lead_id,
            "folioItems": [],
            "newRecommendations": False
        })
    except Exception as error:
        raise RuntimeError(f"Bubble development Folio creation failed: {error}") from error

    subject = {"lead_id": lead_id, "folio_id": folio_id}
    state[case["id"]] = subject
    _save_state(state)
    return subject


def reset_test_subject(case, subject):
    try:
        folio = bubble_get(f"obj/folio/{subject['folio_id']}")
        old_folio_items = list(folio.get("folioItems", []) or [])
        bubble_patch(f"obj/folio/{subject['folio_id']}", {
            "folioItems": [],
            "newRecommendations": False
        })
        for folio_item_id in old_folio_items:
            try:
                bubble_delete(f"obj/folioItem/{folio_item_id}")
            except BubbleTestDataError as error:
                print(
                    f"WARNING: Could not delete old development FolioItem "
                    f"{folio_item_id}: {error}",
                    flush=True
                )
        bubble_patch(f"obj/lead/{subject['lead_id']}", {
            "AIsearchtext": case["initial_ai_searchtext"],
            "AIsearchsummary": ""
        })
    except Exception as error:
        raise RuntimeError(f"Reset failed: {error}") from error


def snapshot_test_subject(subject):
    lead = bubble_get(f"obj/lead/{subject['lead_id']}")
    folio = bubble_get(f"obj/folio/{subject['folio_id']}")
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
