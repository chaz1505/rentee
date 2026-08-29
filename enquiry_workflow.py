"""First-stage WhatsApp control flow for enquiries forwarded by internal Users."""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import re
from typing import Optional


AWAITING_ENQUIRY_FIELD = "Awaiting Enquiry"
PENDING_AGENT_FIELD = "Pending Enquirer Agent?"
AWAITING_SINCE_FIELD = "Awaiting Enquiry Since"
PENDING_ENQUIRY_TTL = timedelta(minutes=30)


@dataclass
class EnquiryWorkflowResult:
    handled: bool
    response_text: Optional[str] = None
    _after_send: object = None

    def complete(self):
        """Commit state changes that must happen only after WhatsApp sends."""
        if self._after_send:
            self._after_send()


def find_internal_user(phone, base_url, bubble_records, bubble_get, normalize_phone):
    """Find a Bubble User by comparing canonical phone values."""
    canonical = normalize_phone(phone)
    if not canonical:
        return None
    constraints = [{
        "key": "phone", "constraint_type": "equals", "value": canonical,
    }]
    checked_ids = set()
    # Prefer the efficient exact lookup when User.phone is stored canonically.
    for user in bubble_records(base_url, "user", constraints):
        user_id = user.get("_id")
        if not user_id:
            continue
        checked_ids.add(str(user_id))
        hydrated = bubble_get(f"{base_url}/obj/user/{user_id}")
        if normalize_phone(hydrated.get("phone")) == canonical:
            return hydrated
    # Existing User records may contain spaces, punctuation, or a leading plus,
    # which Bubble's exact string constraint cannot canonicalize server-side.
    for user in bubble_records(base_url, "user"):
        user_id = user.get("_id")
        if not user_id or str(user_id) in checked_ids:
            continue
        if normalize_phone(user.get("phone")) == canonical:
            return bubble_get(f"{base_url}/obj/user/{user_id}")
    return None


def detect_new_enquiry_instruction(message_text):
    """Return 'agent', 'lead', or None for lightweight setup instructions."""
    text = " ".join(str(message_text or "").casefold().split())
    setup_language = bool(re.search(
        r"\b(next|new|coming|forward|forwarding|send|sending|going to)\b", text
    ))
    if not setup_language:
        return None
    if re.search(r"\bagent\b", text) and re.search(r"\b(enquir|next|one)\w*\b", text):
        return "agent"
    if re.search(r"\blead\b", text) and re.search(r"\b(enquir|next|one|lead)\w*\b", text):
        return "lead"
    return None


def _parse_bubble_date(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_pending_enquiry_state(user):
    awaiting = user.get(AWAITING_ENQUIRY_FIELD)
    if awaiting is not True and str(awaiting or "").strip().casefold() not in {
        "yes", "true",
    }:
        return None
    return {
        "agent": str(user.get(PENDING_AGENT_FIELD) or "").strip() == "Yes",
        "since": _parse_bubble_date(user.get(AWAITING_SINCE_FIELD)),
    }


def is_pending_enquiry_expired(state, now=None):
    now = now or datetime.now(timezone.utc)
    since = state.get("since") if state else None
    return since is None or now - since > PENDING_ENQUIRY_TTL


def _patch_user(user_id, payload, base_url, bubble_patch):
    bubble_patch(f"{base_url}/obj/user/{user_id}", payload)


def set_pending_enquiry(user_id, agent, base_url, bubble_patch, now=None):
    now = now or datetime.now(timezone.utc)
    _patch_user(user_id, {
        AWAITING_ENQUIRY_FIELD: True,
        PENDING_AGENT_FIELD: "Yes" if agent else "No",
        AWAITING_SINCE_FIELD: now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }, base_url, bubble_patch)


def clear_pending_enquiry(user_id, base_url, bubble_patch):
    _patch_user(user_id, {
        AWAITING_ENQUIRY_FIELD: False,
        PENDING_AGENT_FIELD: "",
        AWAITING_SINCE_FIELD: None,
    }, base_url, bubble_patch)
    print(f"[ENQUIRY WORKFLOW] user_id={user_id} pending state cleared", flush=True)


def handle_internal_user_message(
    user, message_text, base_url, bubble_patch, now=None
):
    """Handle one internal User message without depending on Flask or WhatsApp."""
    user_id = user.get("_id")
    if not user_id:
        return EnquiryWorkflowResult(False)
    now = now or datetime.now(timezone.utc)
    pending = get_pending_enquiry_state(user)
    if pending and not is_pending_enquiry_expired(pending, now):
        kind = "agent" if pending["agent"] else "lead"
        print(
            f"[ENQUIRY WORKFLOW] user_id={user_id} pending {kind} enquiry consumed",
            flush=True,
        )
        return EnquiryWorkflowResult(
            True,
            f"Got it — I've received the {kind} enquiry.",
            lambda: clear_pending_enquiry(user_id, base_url, bubble_patch),
        )
    if pending:
        print(f"[ENQUIRY WORKFLOW] user_id={user_id} pending state expired", flush=True)
        clear_pending_enquiry(user_id, base_url, bubble_patch)

    instruction = detect_new_enquiry_instruction(message_text)
    if instruction:
        agent = instruction == "agent"
        print(
            f"[ENQUIRY WORKFLOW] user_id={user_id} {instruction} instruction detected",
            flush=True,
        )
        set_pending_enquiry(user_id, agent, base_url, bubble_patch, now)
        print(f"[ENQUIRY WORKFLOW] user_id={user_id} pending state set", flush=True)
        return EnquiryWorkflowResult(
            True, f"Sure — send me the {instruction} enquiry."
        )
    print(f"[ENQUIRY WORKFLOW] user_id={user_id} not handled", flush=True)
    return EnquiryWorkflowResult(False)
