"""First-stage WhatsApp control flow for enquiries forwarded by internal Users."""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import re
from typing import Optional
from urllib.parse import unquote, urlparse


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


def _extract_urls(text):
    return [
        match.rstrip(".,;:!?)\]}>\"'")
        for match in re.findall(r"https?://[^\s<>()\[\]{}]+", str(text or ""))
    ]


def _normalise_url(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlparse(unquote(value.strip().strip("<>[]()")))
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.netloc.casefold()}{path}"


def _portal_reference(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(unquote(value.strip().strip("<>[]()")))
    except ValueError:
        return None
    host = parsed.netloc.casefold().split(":", 1)[0]
    if not (
        host == "propertyguru.com.my" or host.endswith(".propertyguru.com.my")
        or host == "iproperty.com.my" or host.endswith(".iproperty.com.my")
    ):
        return None
    match = re.search(r"(?:^|/)l/(\d+)(?:/|$)", parsed.path)
    return match.group(1) if match else None


def _normalise_name(value):
    return " ".join(re.sub(r"[^\w]+", " ", str(value or "").casefold()).split())


def _extract_beds(text):
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:beds?|bedrooms?)\b", str(text), re.I)
    return float(match.group(1)) if match else None


def _extract_rent(text):
    value = str(text or "")
    patterns = (
        r"\bRM\s*([\d,]+(?:\.\d+)?)\s*([kK])?",
        r"\b([\d,]+(?:\.\d+)?)\s*([kK])\s*(?:/\s*mo|per\s+month|monthly)?",
        r"\b([\d,]{4,})\s*(?:/\s*mo|per\s+month|monthly)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.I)
        if match:
            amount = float(match.group(1).replace(",", ""))
            if len(match.groups()) > 1 and match.group(2):
                amount *= 1000
            return amount
    return None


def _as_number(value):
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def match_owned_listing(
    message_text, listings, condo_names
):
    """Return (Listing, method, ambiguous candidates) without guessing."""
    message_urls = _extract_urls(message_text)
    normalised_urls = {_normalise_url(url) for url in message_urls}
    references = {_portal_reference(url) for url in message_urls}
    normalised_urls.discard(None)
    references.discard(None)

    direct = [
        listing for listing in listings
        if _normalise_url(listing.get("sourceURL")) in normalised_urls
    ]
    if len(direct) == 1:
        return direct[0], "source_url", []
    if len(direct) > 1:
        return None, None, direct

    referenced = [
        listing for listing in listings
        if _portal_reference(listing.get("sourceURL")) in references
    ]
    if len(referenced) == 1:
        return referenced[0], "portal_reference", []
    if len(referenced) > 1:
        return None, None, referenced

    beds = _extract_beds(message_text)
    rent = _extract_rent(message_text)
    normalised_message = _normalise_name(message_text)
    fallback = []
    if beds is not None and rent is not None:
        for listing in listings:
            condo_name = condo_names.get(str(listing.get("condo") or ""))
            normalised_condo = _normalise_name(condo_name)
            if (
                normalised_condo
                and normalised_condo in normalised_message
                and _as_number(listing.get("beds")) == beds
                and _as_number(listing.get("priceRent")) == rent
            ):
                fallback.append(listing)
    if len(fallback) == 1:
        return fallback[0], "condo_beds_price", []
    return None, None, fallback


def _listing_label(listing, condo_names):
    condo_name = condo_names.get(str(listing.get("condo") or "")) or "listing"
    beds = _as_number(listing.get("beds"))
    rent = _as_number(listing.get("priceRent"))
    parts = [str(condo_name)]
    if beds is not None:
        parts.append(f"{beds:g}-bed")
    if rent is not None:
        parts.append(f"at RM{rent:,.0f}")
    return " ".join(parts)


def consume_pending_enquiry(
    user, pending, message_text, base_url, bubble_create, bubble_records,
    bubble_patch, relationship_names,
):
    """Create the Enquiry, deterministically match an owned Listing, and respond."""
    user_id = user["_id"]
    agent_value = "Yes" if pending["agent"] else "No"
    try:
        enquiry_id = bubble_create(base_url, "enquiry", {
            "Agent": user_id,
            "Agent?": agent_value,
            "Original Enquiry": message_text,
        })
    except Exception as error:
        print(
            f"[ENQUIRY WORKFLOW] user_id={user_id} Enquiry creation failed "
            f"error={type(error).__name__}; pending state retained",
            flush=True,
        )
        raise
    print(f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} created", flush=True)
    # Creation is the durable consumption point. A matching failure must not cause
    # the same forwarded message to create another Enquiry on retry.
    clear_pending_enquiry(user_id, base_url, bubble_patch)

    constraints = [{"key": "owner", "constraint_type": "equals", "value": user_id}]
    owned_listings = [
        listing for listing in bubble_records(base_url, "listing", constraints)
        if listing.get("_id") and str(listing.get("owner") or "") == str(user_id)
    ]
    condo_names = relationship_names(
        base_url, "condo", [listing.get("condo") for listing in owned_listings]
    )
    matched, method, ambiguous = match_owned_listing(
        message_text, owned_listings, condo_names
    )
    if matched:
        bubble_patch(
            f"{base_url}/obj/enquiry/{enquiry_id}", {"Listing": matched["_id"]}
        )
        print(
            f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} "
            f"listing_id={matched['_id']} match_method={method}",
            flush=True,
        )
        label = _listing_label(matched, condo_names)
        response = f"Got it — I've matched this to your {label}."
        if matched.get("availability") is False:
            response += " It's already marked unavailable."
        return EnquiryWorkflowResult(True, response)
    if ambiguous:
        first = ambiguous[0]
        label = _listing_label(first, condo_names)
        print(
            f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} ambiguous_matches={len(ambiguous)}",
            flush=True,
        )
        return EnquiryWorkflowResult(
            True,
            f"I found {len(ambiguous)} of your {label} listings. "
            "Which unit is this enquiry for?",
        )
    print(f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} no listing match", flush=True)
    return EnquiryWorkflowResult(
        True,
        "Got it — I've created the enquiry, but I couldn't confidently match it "
        "to one of your listings. Which listing is this for?",
    )


def handle_internal_user_message(
    user, message_text, base_url, bubble_patch, now=None,
    bubble_create=None, bubble_records=None, relationship_names=None,
):
    """Handle one internal User message without depending on Flask or WhatsApp."""
    user_id = user.get("_id")
    if not user_id:
        return EnquiryWorkflowResult(False)
    now = now or datetime.now(timezone.utc)
    pending = get_pending_enquiry_state(user)
    if pending and not is_pending_enquiry_expired(pending, now):
        print(
            f"[ENQUIRY WORKFLOW] user_id={user_id} pending enquiry consumed",
            flush=True,
        )
        if not all((bubble_create, bubble_records, relationship_names)):
            raise RuntimeError("Pending Enquiry dependencies are not configured.")
        return consume_pending_enquiry(
            user, pending, message_text, base_url, bubble_create, bubble_records,
            bubble_patch, relationship_names,
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
