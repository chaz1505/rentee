"""First-stage WhatsApp control flow for enquiries forwarded by internal Users."""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import re
import secrets
import time
from typing import Optional
from urllib.parse import quote, unquote, urlparse


AWAITING_ENQUIRY_FIELD = "Awaiting Enquiry"
PENDING_AGENT_FIELD = "Pending Enquirer Agent?"
AWAITING_SINCE_FIELD = "Awaiting Enquiry Since"
PENDING_ENQUIRY_TTL = timedelta(minutes=30)
HANDOFF_CODE_PATTERN = re.compile(r"\bRNT-[A-Z0-9]{8}\b", re.I)
HANDOFF_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LEAD_NAME_FIELD = "name"
LEAD_OWNER_FIELD = "owner"
TENANT_PROFILE_REQUEST = """Hi there, thanks for reaching out. Would you be able to share the below info for the owner and let me know when you’d like to view?

TENANT PROFILE
🚩Nationality:
👨‍👩‍👦‍👦Pax (adults/kids/helpers):
🛏️How many rooms do you need?
🪑Furnished or Unfurnished?
💻Occupation:
🐶Pet?
🗓️Start date:
💰Budget:"""
BUYER_PROFILE_REQUEST = """BUYER PROFILE
🚩Nationality:
👨‍👩‍👦‍👦Pax (adults/kids/helpers):
🛏️How many rooms do you need?
🪑Furnished or Unfurnished?
💻Occupation:
🐶Pet?
🗓️Target timing:
💰Budget:"""
RENT_TRANSACTION = "Rent/Let"
BUY_TRANSACTION = "Buy/Sell"
VALID_ENQUIRY_TRANSACTIONS = {RENT_TRANSACTION, BUY_TRANSACTION}
TRANSACTION_CONFIRMATION_REQUEST = "Is this enquiry for rent or purchase?"


@dataclass
class EnquiryWorkflowResult:
    handled: bool
    response_text: Optional[str] = None
    _after_send: object = None
    followup_text: Optional[str] = None
    enquiry_id: Optional[str] = None

    def complete(self):
        """Commit state changes that must happen only after WhatsApp sends."""
        if self._after_send:
            self._after_send()


def extract_handoff_code(message_text):
    match = HANDOFF_CODE_PATTERN.search(str(message_text or ""))
    return match.group(0).upper() if match else None


def explicit_transaction_type(message_text, confirmation=False):
    text = " ".join(str(message_text or "").lower().split())
    rent_patterns = (
        r"\b(?:rent|rental|tenant)\b", r"\bwants? to rent\b",
    )
    buy_patterns = (
        r"\b(?:buyer|buy|purchase)\b", r"\bfor sale\b",
        r"\bwants? to (?:buy|purchase)\b",
    )
    rent = any(re.search(pattern, text) for pattern in rent_patterns)
    buy = any(re.search(pattern, text) for pattern in buy_patterns)
    if rent == buy:
        return None
    if confirmation and len(text.split()) > 8:
        return None
    return RENT_TRANSACTION if rent else BUY_TRANSACTION


def listing_transaction_type(listing):
    def positive_number(value):
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            return False
    listed_types = (listing or {}).get("TransactionType") or []
    if isinstance(listed_types, str):
        listed_types = [listed_types]
    listed_types = {
        value for value in listed_types if value in VALID_ENQUIRY_TRANSACTIONS
    }
    if len(listed_types) == 1:
        return next(iter(listed_types))
    if len(listed_types) > 1:
        return None
    has_rent = positive_number((listing or {}).get("priceRent"))
    has_sale = positive_number((listing or {}).get("priceSale"))
    if has_rent == has_sale:
        return None
    return RENT_TRANSACTION if has_rent else BUY_TRANSACTION


def enquiry_transaction_type(enquiry):
    values = (enquiry or {}).get("TransactionType") or []
    if isinstance(values, str):
        values = [values]
    valid = [value for value in values if value in VALID_ENQUIRY_TRANSACTIONS]
    return valid[0] if len(set(valid)) == 1 else None


def merged_lead_transaction_types(lead, enquiry):
    """Return a cumulative valid Lead transaction list, or None if unchanged."""
    existing = (lead or {}).get("TransactionType") or []
    if isinstance(existing, str):
        existing = [existing]
    merged = []
    for value in existing:
        if value in VALID_ENQUIRY_TRANSACTIONS and value not in merged:
            merged.append(value)
    current = enquiry_transaction_type(enquiry)
    if not current or current in merged:
        return None
    merged.append(current)
    return merged


def build_enquiry_creation_payload(user_id, agent_value, message_text,
                                   transaction_type=None):
    payload = {
        "Agent": user_id,
        "Agent?": agent_value,
        "Original Enquiry": message_text,
    }
    if transaction_type in VALID_ENQUIRY_TRANSACTIONS:
        # Bubble Data API text-list fields use native JSON arrays.
        payload["TransactionType"] = [transaction_type]
    return payload


def _enquiry_creation_failure_details(error, payload):
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    try:
        body = (
            " ".join(str(response.text or "").split())
            if response is not None else ""
        )
    except Exception:
        body = ""
    original_enquiry = str(payload.get("Original Enquiry") or "")
    if original_enquiry:
        body = body.replace(original_enquiry, "<redacted>")
    transaction_value = payload.get("TransactionType")
    return (
        status,
        body[:1000] or type(error).__name__,
        type(transaction_value).__name__ if "TransactionType" in payload else "omitted",
    )


def _clean_person_name(value, minimum_words=1):
    candidate = " ".join(str(value or "").strip().split())
    candidate = re.split(
        r"[.!?\n]|\s+(?:and\s+I|I(?:'m|\s+am)|interested\b|calling\b)",
        candidate,
        maxsplit=1,
        flags=re.I,
    )[0].strip(" ,;:-")
    words = candidate.split()
    if not minimum_words <= len(words) <= 4:
        return None
    if not all(re.fullmatch(r"[A-Z][A-Za-z'’\-]*", word) for word in words):
        return None
    return candidate


def extract_enquirer_name(message_text):
    """Extract only explicit, first-person sender-name declarations."""
    text = str(message_text or "")
    patterns = (
        r"\bName\s*:\s*([^\r\n]+)",
        r"\b(?:I['’]m|I\s+am|this\s+is)\s+([^\r\n.!?]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            candidate = _clean_person_name(match.group(1), minimum_words=2)
            if candidate:
                return candidate
    return None


def _with_agent_suffix(name):
    value = str(name or "").strip()
    if not value:
        return value, "none"
    if re.search(r"\s*\(agent\)\s*$", value, re.I):
        return value, "already_present"
    return f"{value} (Agent)", "appended"


def _valid_handoff_code(value):
    return bool(HANDOFF_CODE_PATTERN.fullmatch(str(value or "").strip()))


def _new_handoff_code():
    return "RNT-" + "".join(secrets.choice(HANDOFF_CODE_ALPHABET) for _ in range(8))


def ensure_handoff_code(
    enquiry_id, enquiry, base_url, bubble_records, bubble_patch
):
    existing = str((enquiry or {}).get("Handoff Code") or "").strip().upper()
    if _valid_handoff_code(existing):
        return existing
    while True:
        code = _new_handoff_code()
        constraints = [{
            "key": "Handoff Code", "constraint_type": "equals", "value": code,
        }]
        if not any(bubble_records(base_url, "enquiry", constraints)):
            bubble_patch(
                f"{base_url}/obj/enquiry/{enquiry_id}", {"Handoff Code": code}
            )
            print(
                f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} handoff_code_created",
                flush=True,
            )
            return code


def build_whatsapp_handoff_link(code, rentee_whatsapp_number, normalize_phone):
    configured_number = str(rentee_whatsapp_number or "").strip()
    if not configured_number or not re.fullmatch(
        r"\+?[0-9\s()\-]+", configured_number
    ):
        raise ValueError("Rentee WhatsApp dialable number is not configured.")
    number = normalize_phone(configured_number)
    if not number or not number.isdigit():
        raise ValueError("Rentee WhatsApp dialable number is not configured.")
    message = f"Hi, I'm following up on enquiry {code}"
    return f"https://wa.me/{number}?text={quote(message, safe='')}"


def handle_external_handoff_message(
    sender_phone, message_text, base_url, bubble_records, bubble_get,
    bubble_patch, normalize_phone, sender_user_id=None,
    find_or_create_lead=None, whatsapp_profile_name=None,
):
    """Resolve and bind one external WhatsApp sender to an existing Enquiry."""
    code = extract_handoff_code(message_text)
    if not code:
        return EnquiryWorkflowResult(False)
    print(f"[ENQUIRY WORKFLOW] handoff token detected code={code}", flush=True)
    constraints = [{
        "key": "Handoff Code", "constraint_type": "equals", "value": code,
    }]
    matches = list(bubble_records(base_url, "enquiry", constraints))
    print(
        f"[ENQUIRY WORKFLOW] handoff lookup code={code} "
        f"result_count={len(matches)}",
        flush=True,
    )
    if len(matches) != 1 or not matches[0].get("_id"):
        print(
            f"[ENQUIRY WORKFLOW] handoff code resolution inconsistent matches={len(matches)}",
            flush=True,
        )
        return EnquiryWorkflowResult(
            True, "I couldn't connect this enquiry automatically. Please ask the agent "
            "who sent you the link to send you a fresh one."
        )
    enquiry_id = matches[0]["_id"]
    enquiry = bubble_get(f"{base_url}/obj/enquiry/{enquiry_id}")
    print(
        f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} handoff resolved",
        flush=True,
    )
    if sender_user_id and str(enquiry.get("Agent") or "") == str(sender_user_id):
        print(
            f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} "
            "handoff rejected reason=originating_agent",
            flush=True,
        )
        return EnquiryWorkflowResult(
            True, "This handoff link is for the enquirer. Please send it to them "
            "so they can continue with Rentee."
        )
    listing_id = enquiry.get("Listing")
    if not listing_id:
        print(
            f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} incomplete handoff missing Listing",
            flush=True,
        )
        return EnquiryWorkflowResult(
            True, "I couldn't connect this enquiry automatically. Please ask the agent "
            "who sent you the link to send you a fresh one."
        )
    try:
        bubble_get(f"{base_url}/obj/listing/{listing_id}")
    except Exception as error:
        print(
            f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} handoff Listing unavailable "
            f"error={type(error).__name__}",
            flush=True,
        )
        return EnquiryWorkflowResult(
            True, "I couldn't connect this enquiry automatically. Please ask the agent "
            "who sent you the link to send you a fresh one."
        )
    incoming_phone = normalize_phone(sender_phone)
    existing_phone = normalize_phone(enquiry.get("Enquirer Phone"))
    safe_incoming = f"...{incoming_phone[-4:]}" if incoming_phone else "unknown"
    if existing_phone and existing_phone != incoming_phone:
        safe_existing = f"...{existing_phone[-4:]}"
        print(
            f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} handoff conflict "
            f"existing_phone={safe_existing} incoming_phone={safe_incoming}",
            flush=True,
        )
        return EnquiryWorkflowResult(
            True, "I couldn't connect this enquiry automatically. Please ask the agent "
            "who sent you the link to send you a fresh one."
        )
    if not existing_phone:
        bubble_patch(
            f"{base_url}/obj/enquiry/{enquiry_id}",
            {"Enquirer Phone": incoming_phone},
        )
        print(
            f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} "
            f"enquirer_phone_set phone={safe_incoming}",
            flush=True,
        )
    if find_or_create_lead:
        enquiry_classification = (
            "Yes" if str(enquiry.get("Agent?") or "").strip() == "Yes" else "No"
        )
        extracted_name = extract_enquirer_name(enquiry.get("Original Enquiry"))
        profile_name = _clean_person_name(whatsapp_profile_name)
        candidate_name = extracted_name or profile_name
        name_source = (
            "original_enquiry" if extracted_name else
            "whatsapp_profile" if profile_name else "none"
        )
        if enquiry_classification == "Yes" and candidate_name:
            candidate_name, _suffix_action = _with_agent_suffix(candidate_name)
        try:
            lead, lead_created = find_or_create_lead(
                incoming_phone,
                customer_name=candidate_name,
                agent_classification=enquiry_classification,
                owner_user_id=enquiry.get("Agent"),
            )
            lead_id = lead.get("_id")
            if not lead_id:
                raise ValueError("Lead lookup/create returned no ID.")
            print(
                f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} lead lookup "
                f"phone={safe_incoming} "
                f"result={'created' if lead_created else 'existing'}",
                flush=True,
            )
            linked_lead_id = enquiry.get("Lead")
            if linked_lead_id and str(linked_lead_id) != str(lead_id):
                print(
                    f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} lead conflict "
                    f"existing_lead_id={linked_lead_id} resolved_lead_id={lead_id}",
                    flush=True,
                )
                return EnquiryWorkflowResult(
                    True, "I couldn't connect this enquiry automatically. Please ask "
                    "the agent who sent you the link to send you a fresh one."
                )

            merged_transactions = merged_lead_transaction_types(lead, enquiry)
            if merged_transactions is not None:
                bubble_patch(
                    f"{base_url}/obj/lead/{lead_id}",
                    {"TransactionType": merged_transactions},
                )
                lead["TransactionType"] = merged_transactions
                print(
                    f"[ENQUIRY WORKFLOW] lead_id={lead_id} "
                    "transaction_types_merged",
                    flush=True,
                )

            existing_classification = str(lead.get("Agent?") or "").strip()
            action = "kept"
            if existing_classification != "Yes" and (
                existing_classification not in {"Yes", "No"}
                or enquiry_classification == "Yes"
            ):
                bubble_patch(
                    f"{base_url}/obj/lead/{lead_id}",
                    {"Agent?": enquiry_classification},
                )
                lead["Agent?"] = enquiry_classification
                action = (
                    "upgraded" if existing_classification == "No" else "set"
                )
            final_classification = str(lead.get("Agent?") or enquiry_classification)
            print(
                f"[ENQUIRY WORKFLOW] lead_id={lead_id} "
                f"agent_classification={final_classification} action={action}",
                flush=True,
            )
            existing_name = str(lead.get(LEAD_NAME_FIELD) or "").strip()
            final_name = existing_name
            name_action = "unchanged"
            if not final_name and candidate_name:
                final_name = candidate_name
                bubble_patch(
                    f"{base_url}/obj/lead/{lead_id}",
                    {LEAD_NAME_FIELD: final_name},
                )
                lead[LEAD_NAME_FIELD] = final_name
                name_action = "set"
            elif final_name and not lead_created:
                name_source = "existing"
                name_action = "kept"
            elif final_name:
                name_action = "set"
            print(
                f"[ENQUIRY WORKFLOW] lead_id={lead_id} name_source={name_source} "
                f"action={name_action}",
                flush=True,
            )
            if final_classification == "Yes" and final_name:
                suffixed_name, suffix_action = _with_agent_suffix(final_name)
                if suffix_action == "appended":
                    bubble_patch(
                        f"{base_url}/obj/lead/{lead_id}",
                        {LEAD_NAME_FIELD: suffixed_name},
                    )
                    lead[LEAD_NAME_FIELD] = suffixed_name
                print(
                    f"[ENQUIRY WORKFLOW] lead_id={lead_id} "
                    f"agent_suffix={suffix_action}",
                    flush=True,
                )
            enquiry_agent_user_id = str(enquiry.get("Agent") or "").strip()
            existing_owner_user_id = str(lead.get(LEAD_OWNER_FIELD) or "").strip()
            if enquiry_agent_user_id:
                if not existing_owner_user_id:
                    if not lead_created:
                        bubble_patch(
                            f"{base_url}/obj/lead/{lead_id}",
                            {LEAD_OWNER_FIELD: enquiry_agent_user_id},
                        )
                    lead[LEAD_OWNER_FIELD] = enquiry_agent_user_id
                    print(
                        f"[ENQUIRY WORKFLOW] lead_id={lead_id} "
                        f"owner_user_id={enquiry_agent_user_id} action=set",
                        flush=True,
                    )
                elif existing_owner_user_id == enquiry_agent_user_id:
                    print(
                        f"[ENQUIRY WORKFLOW] lead_id={lead_id} "
                        f"owner_user_id={enquiry_agent_user_id} action=kept",
                        flush=True,
                    )
                else:
                    print(
                        f"[ENQUIRY WORKFLOW] lead_id={lead_id} "
                        f"existing_owner_user_id={existing_owner_user_id} "
                        f"enquiry_agent_user_id={enquiry_agent_user_id} "
                        "action=owner_conflict_preserved",
                        flush=True,
                    )
            if not linked_lead_id:
                bubble_patch(
                    f"{base_url}/obj/enquiry/{enquiry_id}", {"Lead": lead_id}
                )
            bubble_patch(
                f"{base_url}/obj/lead/{lead_id}",
                {"ActiveForwardedEnquiry": enquiry_id},
            )
            lead["ActiveForwardedEnquiry"] = enquiry_id
            print(
                f"[ENQUIRY WORKFLOW] lead_id={lead_id} "
                f"active_forwarded_enquiry_id={enquiry_id} action=set",
                flush=True,
            )
            print(
                f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} "
                f"lead_id={lead_id} lead linked",
                flush=True,
            )
        except Exception as error:
            print(
                f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} Lead linking failed "
                f"error={type(error).__name__}",
                flush=True,
            )
            return EnquiryWorkflowResult(
                True, "I couldn't connect this enquiry automatically. Please ask the "
                "agent who sent you the link to send you a fresh one."
            )
    print(f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} handoff completed", flush=True)
    transaction_type = enquiry_transaction_type(enquiry)
    followup_text = (
        TENANT_PROFILE_REQUEST if transaction_type == RENT_TRANSACTION else
        BUYER_PROFILE_REQUEST if transaction_type == BUY_TRANSACTION else None
    )
    return EnquiryWorkflowResult(
        True, "Hi — I've got your enquiry for this property. I'll help you from here.",
        followup_text=followup_text,
        enquiry_id=enquiry_id,
    )


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
    agent_indication = bool(re.search(r"\bagent\b", text)) and (
        setup_language
        or bool(re.search(r"\banother agent is enquir\w*\b", text))
        or bool(re.search(r"\benquir\w*\s+is\s+from\s+an?\s+agent\b", text))
        or bool(re.search(r"\bagent\s+(?:lead|enquir\w*|incoming|coming through)\b", text))
    )
    if agent_indication:
        return "agent"
    lead_indication = (
        setup_language and bool(re.search(r"\b(?:lead|customer|tenant)\b", text))
    ) or bool(re.search(
        r"\b(?:direct lead|customer enquir\w*|tenant enquir\w*)\b", text
    ))
    if lead_indication:
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
    urls = []
    for match in re.findall(r"https?://[^\s<>()\[\]{}\"']+", str(text or "")):
        clean = match.rstrip(".,;:!?)\]}>\"'")
        if clean and clean not in urls:
            urls.append(clean)
    return urls


def _coerce_url(value):
    if not isinstance(value, str) or not value.strip():
        return None
    extracted = _extract_urls(value)
    return extracted[0] if extracted else value.strip().strip("<>[]()\"'")


def _normalise_url(value):
    value = _coerce_url(value)
    if not value:
        return None
    try:
        parsed = urlparse(unquote(value))
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.netloc.casefold()}{path}"


def _portal_reference(value):
    value = _coerce_url(value)
    if not value:
        return None
    try:
        parsed = urlparse(unquote(value))
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
    if references:
        print(
            f"[ENQUIRY WORKFLOW] portal_references={sorted(references)}",
            flush=True,
        )

    beds = _extract_beds(message_text)
    rent = _extract_rent(message_text)
    normalised_message = _normalise_name(message_text)

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


def _matching_log_context(message_text, condo_names):
    references = sorted({
        reference for reference in (
            _portal_reference(url) for url in _extract_urls(message_text)
        ) if reference
    })
    normalised_message = _normalise_name(message_text)
    parsed_condos = [
        name for name in condo_names.values()
        if _normalise_name(name) and _normalise_name(name) in normalised_message
    ]
    return references, parsed_condos[:3], _extract_beds(message_text), _extract_rent(message_text)


def _ambiguous_match_method(message_text, listings):
    urls = {_normalise_url(url) for url in _extract_urls(message_text)}
    references = {_portal_reference(url) for url in _extract_urls(message_text)}
    if any(_normalise_url(listing.get("sourceURL")) in urls for listing in listings):
        return "source_url"
    if any(_portal_reference(listing.get("sourceURL")) in references for listing in listings):
        return "portal_reference"
    return "condo_beds_price"


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


def _portal_references(message_text):
    return sorted({
        reference for reference in (
            _portal_reference(url) for url in _extract_urls(message_text)
        ) if reference
    })


def _fast_portal_listing_match(
    message_text, user_id, base_url, bubble_records, bubble_get
):
    """Return one owner-constrained Listing found by stable portal reference."""
    references = _portal_references(message_text)
    if not references:
        return None, 0
    print(f"[ENQUIRY WORKFLOW] portal_references={references}", flush=True)
    candidates = {}
    for reference in references:
        started = time.perf_counter()
        constraints = [
            {"key": "owner", "constraint_type": "equals", "value": user_id},
            {
                "key": "sourceURL", "constraint_type": "text contains",
                "value": reference,
            },
        ]
        try:
            results = list(bubble_records(base_url, "listing", constraints))
        except Exception as error:
            print(
                f"[ENQUIRY WORKFLOW] portal fast lookup reference={reference} "
                f"duration_ms={(time.perf_counter() - started) * 1000:.1f} "
                f"error={type(error).__name__} falling_back=true",
                flush=True,
            )
            return None, 0
        print(
            f"[ENQUIRY WORKFLOW] portal fast lookup reference={reference} "
            f"result_count={len(results)} "
            f"duration_ms={(time.perf_counter() - started) * 1000:.1f}",
            flush=True,
        )
        for result in results[:3]:
            print(
                "[ENQUIRY WORKFLOW] portal fast candidate "
                f"listing_id={result.get('_id')} "
                f"source_url={result.get('sourceURL')}",
                flush=True,
            )
        for result in results:
            listing_id = result.get("_id")
            returned_owner = result.get("owner")
            if (
                listing_id
                and (not returned_owner or str(returned_owner) == str(user_id))
            ):
                candidates[str(listing_id)] = dict(result)

    if len(candidates) != 1:
        outcome = "zero_results" if not candidates else "multiple_results"
        print(
            "[ENQUIRY WORKFLOW] portal fast lookup no_unique_match "
            f"references={references} reason={outcome} falling_back=true",
            flush=True,
        )
        return None, 0

    listing_id, candidate = next(iter(candidates.items()))
    hydration_count = 0
    if bubble_get and not candidate.get("sourceURL"):
        try:
            hydration_count += 1
            hydrated = bubble_get(f"{base_url}/obj/listing/{listing_id}")
            if isinstance(hydrated, dict):
                candidate.update(hydrated)
        except Exception as error:
            print(
                f"[ENQUIRY WORKFLOW] listing_id={listing_id} fast-path hydration "
                f"failed error={type(error).__name__}; falling_back=true",
                flush=True,
            )
            return None, hydration_count
    candidate.setdefault("_id", listing_id)
    returned_owner = candidate.get("owner")
    if returned_owner and str(returned_owner) != str(user_id):
        print(
            "[ENQUIRY WORKFLOW] portal fast lookup owner mismatch falling_back=true",
            flush=True,
        )
        return None, hydration_count
    stored_reference = _portal_reference(candidate.get("sourceURL"))
    if stored_reference and stored_reference not in references:
        print(
            "[ENQUIRY WORKFLOW] portal fast lookup reference mismatch falling_back=true",
            flush=True,
        )
        return None, hydration_count
    return candidate, hydration_count


def consume_pending_enquiry(
    user, pending, message_text, base_url, bubble_create, bubble_records,
    bubble_patch, relationship_names, bubble_get=None, normalize_phone=None,
    rentee_whatsapp_number=None,
):
    """Create the Enquiry, deterministically match an owned Listing, and respond."""
    user_id = user["_id"]
    agent_value = "Yes" if pending["agent"] else "No"
    explicit_transaction = explicit_transaction_type(message_text)
    creation_payload = build_enquiry_creation_payload(
        user_id, agent_value, message_text, explicit_transaction
    )
    try:
        enquiry_id = bubble_create(base_url, "enquiry", creation_payload)
    except Exception as error:
        status, bubble_error, transaction_type_type = (
            _enquiry_creation_failure_details(error, creation_payload)
        )
        if status is not None:
            print(
                f"[ENQUIRY WORKFLOW] user_id={user_id} Enquiry creation failed "
                f"status={status} fields={list(creation_payload)} "
                f"transaction_type_type={transaction_type_type} "
                f"bubble_error={bubble_error!r}; pending state retained",
                flush=True,
            )
        else:
            print(
                f"[ENQUIRY WORKFLOW] user_id={user_id} Enquiry creation failed "
                f"error={type(error).__name__} fields={list(creation_payload)} "
                f"transaction_type_type={transaction_type_type}; "
                "pending state retained",
                flush=True,
            )
        raise
    print(f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} created", flush=True)
    # Creation is the durable consumption point. A matching failure must not cause
    # the same forwarded message to create another Enquiry on retry.
    clear_pending_enquiry(user_id, base_url, bubble_patch)

    matched, hydration_count = _fast_portal_listing_match(
        message_text, user_id, base_url, bubble_records, bubble_get
    )
    method = "portal_reference_fast_path" if matched else None
    ambiguous = []
    owned_listings = []
    fallback_started = None
    if not matched:
        fallback_started = time.perf_counter()
        constraints = [
            {"key": "owner", "constraint_type": "equals", "value": user_id}
        ]
        print(
            "[ENQUIRY WORKFLOW] retrieving owned Listings "
            f"object_type=listing constraint_key=owner user_id={user_id}",
            flush=True,
        )
        try:
            constrained_listings = list(
                bubble_records(base_url, "listing", constraints)
            )
        except Exception as error:
            print(
                "[ENQUIRY WORKFLOW] owned Listing retrieval failed "
                f"error={type(error).__name__}",
                flush=True,
            )
            raise
        for listing in constrained_listings:
            listing_id = listing.get("_id")
            if not listing_id:
                print(
                    "[ENQUIRY WORKFLOW] ignored constrained Listing without ID",
                    flush=True,
                )
                continue
            candidate = dict(listing)
            returned_owner = candidate.get("owner")
            if returned_owner and str(returned_owner) != str(user_id):
                print(
                    f"[ENQUIRY WORKFLOW] listing_id={listing_id} rejected owner mismatch",
                    flush=True,
                )
                continue
            owned_listings.append(candidate)
        print(
            f"[ENQUIRY WORKFLOW] owned_listings_count={len(owned_listings)} "
            f"constrained_results={len(constrained_listings)}",
            flush=True,
        )
    if not matched:
        condo_names = relationship_names(
            base_url, "condo", [listing.get("condo") for listing in owned_listings]
        )
        matched, method, ambiguous = match_owned_listing(
            message_text, owned_listings, condo_names
        )
        if matched and bubble_get and any(
            matched.get(field) in (None, "")
            for field in ("sourceURL", "condo", "beds", "priceRent")
        ):
            try:
                hydration_count += 1
                hydrated = bubble_get(
                    f"{base_url}/obj/listing/{matched['_id']}"
                )
                if isinstance(hydrated, dict):
                    matched = {**matched, **hydrated}
                    matched.setdefault("_id", hydrated.get("_id") or matched["_id"])
            except Exception as error:
                print(
                    f"[ENQUIRY WORKFLOW] listing_id={matched['_id']} hydration failed "
                    f"error={type(error).__name__}; using collection record",
                    flush=True,
                )
        if (
            matched and matched.get("owner")
            and str(matched["owner"]) != str(user_id)
        ):
            print(
                f"[ENQUIRY WORKFLOW] listing_id={matched['_id']} "
                "rejected owner mismatch after hydration",
                flush=True,
            )
            matched = None
        print(
            "[ENQUIRY WORKFLOW] broad listing fallback "
            f"duration_ms={(time.perf_counter() - fallback_started) * 1000:.1f}",
            flush=True,
        )
    print(
        f"[ENQUIRY WORKFLOW] listing match hydration_count={hydration_count}",
        flush=True,
    )
    condo_names = relationship_names(
        base_url, "condo", [
            listing.get("condo")
            for listing in ([matched] if matched else owned_listings)
        ]
    )
    if matched:
        transaction_type = explicit_transaction or listing_transaction_type(matched)
        matched_listing_id = str(matched["_id"])
        condo_id = str(matched.get("condo") or "")
        print(
            f"[FORWARDED ENQUIRY] enquiry_id={enquiry_id} "
            f"matched_listing_id={matched_listing_id} condo_id={condo_id}",
            flush=True,
        )
        enquiry_update = {"Listing": matched_listing_id}
        if transaction_type:
            enquiry_update["TransactionType"] = [transaction_type]
        bubble_patch(f"{base_url}/obj/enquiry/{enquiry_id}", enquiry_update)
        print(
            f"[FORWARDED ENQUIRY] enquiry_id={enquiry_id} "
            f"listing_relationship_written={matched_listing_id}", flush=True,
        )
        print(
            f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} "
            f"listing_id={matched_listing_id} match_method={method}",
            flush=True,
        )
        availability = matched.get("availability")
        availability_label = (
            "true" if availability is True else
            "false" if availability is False else "unknown"
        )
        print(
            f"[ENQUIRY WORKFLOW] listing_id={matched['_id']} "
            f"availability={availability_label}",
            flush=True,
        )
        label = _listing_label(matched, condo_names)
        response = f"Got it — I've matched this to your {label}."
        if not transaction_type:
            print(
                f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} "
                "transaction_type_unresolved",
                flush=True,
            )
            return EnquiryWorkflowResult(True, TRANSACTION_CONFIRMATION_REQUEST)
        if availability is False:
            print(
                f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} "
                "handoff_eligible=false reason=listing_unavailable",
                flush=True,
            )
            return EnquiryWorkflowResult(
                True, f"The {label} is marked as unavailable."
            )

        if availability is not True:
            print(
                f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} "
                "handoff_eligible=true availability_not_explicitly_false",
                flush=True,
            )
        else:
            print(
                f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} handoff_eligible=true",
                flush=True,
            )
        enquiry = (
            bubble_get(f"{base_url}/obj/enquiry/{enquiry_id}")
            if bubble_get else {}
        )
        code = ensure_handoff_code(
            enquiry_id, enquiry, base_url, bubble_records, bubble_patch
        )
        link = build_whatsapp_handoff_link(
            code, rentee_whatsapp_number, normalize_phone
        )
        print(
            f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} handoff_link_generated",
            flush=True,
        )
        response += (
            "\n\nSend this link to the enquirer so they can continue with Rentee:\n"
            f"{link}"
        )
        return EnquiryWorkflowResult(True, response)
    if ambiguous:
        first = ambiguous[0]
        label = _listing_label(first, condo_names)
        ambiguous_method = _ambiguous_match_method(message_text, ambiguous)
        listing_ids = [listing.get("_id") for listing in ambiguous if listing.get("_id")]
        print(
            f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} ambiguous listing match "
            f"method={ambiguous_method} listing_ids={listing_ids}",
            flush=True,
        )
        print(
            f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} "
            "handoff_eligible=false reason=ambiguous_listing",
            flush=True,
        )
        return EnquiryWorkflowResult(
            True,
            f"I found {len(ambiguous)} of your {label} listings. "
            "Which unit is this enquiry for?",
        )
    references, parsed_condos, parsed_beds, parsed_rent = _matching_log_context(
        message_text, condo_names
    )
    print(
        f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} no listing match "
        f"portal_refs={references} parsed_condos={parsed_condos} "
        f"parsed_beds={parsed_beds} parsed_rent={parsed_rent} "
        f"owned_listings_count={len(owned_listings)}",
        flush=True,
    )
    print(
        f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} "
        "handoff_eligible=false reason=no_listing_match",
        flush=True,
    )
    return EnquiryWorkflowResult(
        True,
        "Got it — I've created the enquiry, but I couldn't confidently match it "
        "to one of your listings. Which listing is this for?",
    )


def handle_internal_user_message(
    user, message_text, base_url, bubble_patch, now=None,
    bubble_create=None, bubble_records=None, relationship_names=None,
    bubble_get=None, normalize_phone=None, rentee_whatsapp_number=None,
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
            bubble_patch, relationship_names, bubble_get, normalize_phone,
            rentee_whatsapp_number,
        )
    if pending:
        print(f"[ENQUIRY WORKFLOW] user_id={user_id} pending state expired", flush=True)
        clear_pending_enquiry(user_id, base_url, bubble_patch)

    confirmation = explicit_transaction_type(message_text, confirmation=True)
    if confirmation and bubble_records and bubble_get:
        constraints = [{
            "key": "Agent", "constraint_type": "equals", "value": user_id,
        }]
        unresolved = []
        for candidate in bubble_records(base_url, "enquiry", constraints):
            if candidate.get("_id") and candidate.get("Listing") \
                    and not candidate.get("Handoff Code") \
                    and not enquiry_transaction_type(candidate):
                unresolved.append(candidate)
        if len(unresolved) == 1:
            enquiry = unresolved[0]
            enquiry_id = enquiry["_id"]
            bubble_patch(
                f"{base_url}/obj/enquiry/{enquiry_id}",
                {"TransactionType": [confirmation]},
            )
            listing = bubble_get(
                f"{base_url}/obj/listing/{enquiry['Listing']}"
            )
            if listing.get("availability") is False:
                return EnquiryWorkflowResult(
                    True, "That listing is marked as unavailable."
                )
            hydrated = bubble_get(f"{base_url}/obj/enquiry/{enquiry_id}")
            hydrated["TransactionType"] = [confirmation]
            code = ensure_handoff_code(
                enquiry_id, hydrated, base_url, bubble_records, bubble_patch
            )
            link = build_whatsapp_handoff_link(
                code, rentee_whatsapp_number, normalize_phone
            )
            print(
                f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} "
                f"transaction_type_confirmed value={confirmation}", flush=True,
            )
            return EnquiryWorkflowResult(
                True,
                "Got it. Send this link to the enquirer so they can continue "
                f"with Rentee:\n{link}",
            )

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
