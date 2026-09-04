"""Durable Bubble Conversation primitives for incremental WhatsApp migration."""

import datetime
import json
import os
import re

import requests


def normalize_phone(value):
    return re.sub(r"\D", "", str(value or ""))


def relationship_id(value):
    """Extract one Bubble relationship ID without reinterpreting other values."""
    if isinstance(value, dict):
        value = value.get("_id") or value.get("id")
    if isinstance(value, (str, int)):
        return str(value).strip() or None
    return None


def get_bubble_base_url(bubble_env="live"):
    if bubble_env == "development":
        return "https://www.rentee.asia/version-test/api/1.1"
    return "https://www.rentee.asia/api/1.1"


def _headers():
    return {
        "Authorization": f"Bearer {os.environ['BUBBLE_API_TOKEN']}",
        "Content-Type": "application/json",
    }


def _safe_bubble_error(error):
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    try:
        body = (
            " ".join(str(response.text or "").split())
            if response is not None else ""
        )
    except Exception:
        body = ""
    body = re.sub(r"\b\d{7,}\b", "<redacted-number>", body)
    return status, body[:1000] or type(error).__name__


def _log_http_failure(error, operation, method, object_type):
    status, bubble_error = _safe_bubble_error(error)
    print(
        f"[CONVERSATION] action=failed operation={operation} "
        f"method={method} object_type={object_type} "
        f"status={status if status is not None else 'unknown'} "
        f"bubble_error={bubble_error!r}",
        flush=True,
    )


def _records(base_url, object_type, constraints=None):
    cursor = 0
    while True:
        params = {"cursor": cursor}
        if constraints:
            params["constraints"] = json.dumps(constraints, separators=(",", ":"))
        try:
            response = requests.get(
                f"{base_url}/obj/{object_type}", headers=_headers(), params=params,
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            _log_http_failure(error, "find", "GET", object_type)
            raise
        page = response.json()["response"]
        results = page.get("results", []) or []
        yield from results
        if not results or not page.get("remaining"):
            break
        cursor += len(results)


def _get(base_url, object_type, object_id):
    try:
        response = requests.get(
            f"{base_url}/obj/{object_type}/{object_id}", headers=_headers(),
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        _log_http_failure(error, "get", "GET", object_type)
        raise
    return response.json()["response"]


def _create(base_url, object_type, payload):
    try:
        response = requests.post(
            f"{base_url}/obj/{object_type}", headers=_headers(), json=payload,
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        _log_http_failure(error, "create", "POST", object_type)
        raise
    object_id = response.json().get("id")
    if not object_id:
        raise ValueError(f"Bubble did not return a {object_type} ID.")
    return object_id


def _patch(base_url, object_type, object_id, payload):
    try:
        response = requests.patch(
            f"{base_url}/obj/{object_type}/{object_id}", headers=_headers(),
            json=payload, timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        _log_http_failure(error, "update", "PATCH", object_type)
        raise
    return response


def find_active_conversation(
    principal_id, counterparty_phone, enquiry_id=None, bubble_env="live", side=None,
):
    principal_id = relationship_id(principal_id)
    phone = normalize_phone(counterparty_phone)
    enquiry_id = relationship_id(enquiry_id)
    if not principal_id or not phone:
        return None
    constraints = [
        {"key": "Principal", "constraint_type": "equals", "value": principal_id},
        {"key": "CounterParty Phone", "constraint_type": "equals", "value": phone},
        {"key": "Status", "constraint_type": "equals", "value": "Active"},
    ]
    if enquiry_id:
        constraints.append({
            "key": "Enquiry", "constraint_type": "equals", "value": enquiry_id,
        })
    print(
        f"[CONVERSATION] side={side or 'unspecified'} action=find_started "
        f"principal_id={principal_id} "
        f"enquiry_id={enquiry_id or 'none'}", flush=True,
    )
    candidates = list(_records(
        get_bubble_base_url(bubble_env), "conversation", constraints
    ))
    matches = []
    for conversation in candidates:
        candidate_enquiry = relationship_id(conversation.get("Enquiry"))
        if candidate_enquiry != enquiry_id:
            continue
        if relationship_id(conversation.get("Principal")) != principal_id:
            continue
        if normalize_phone(conversation.get("CounterParty Phone")) != phone:
            continue
        if str(conversation.get("Status") or "").strip() != "Active":
            continue
        if conversation.get("_id"):
            matches.append(conversation)
    if not matches:
        return None
    matches.sort(key=lambda item: str(item.get("_id")))
    selected = matches[0]
    print(
        f"[CONVERSATION] side={side or 'unspecified'} action=found "
        f"conversation_id={selected['_id']} "
        f"principal_id={principal_id} enquiry_id={enquiry_id or 'none'}",
        flush=True,
    )
    return selected


def find_active_general_conversation(
    counterparty_phone, lead_id=None, counterparty_user_id=None,
    bubble_env="live", side=None,
):
    """Find an active non-enquiry Conversation for an unassigned Lead."""
    phone = normalize_phone(counterparty_phone)
    lead_id = relationship_id(lead_id)
    counterparty_user_id = relationship_id(counterparty_user_id)
    if not phone or (not lead_id and not counterparty_user_id):
        return None
    constraints = [
        {"key": "CounterParty Phone", "constraint_type": "equals", "value": phone},
        {"key": "Status", "constraint_type": "equals", "value": "Active"},
    ]
    matches = []
    for item in _records(
        get_bubble_base_url(bubble_env), "conversation", constraints
    ):
        if not item.get("_id") or relationship_id(item.get("Enquiry")):
            continue
        visible_phone = normalize_phone(item.get("CounterParty Phone"))
        visible_status = str(item.get("Status") or "").strip()
        visible_lead = relationship_id(item.get("Lead"))
        visible_user = relationship_id(item.get("Counterparty User"))
        if visible_phone and visible_phone != phone:
            continue
        if visible_status and visible_status != "Active":
            continue
        if lead_id and visible_lead and visible_lead != lead_id:
            continue
        if counterparty_user_id and visible_user and visible_user != counterparty_user_id:
            continue
        matches.append(item)
    if not matches:
        return None
    matches.sort(key=lambda item: str(item.get("_id")))
    selected = matches[0]
    print(
        f"[CONVERSATION] side={side or 'unspecified'} action=found "
        f"type=general conversation_id={selected['_id']} "
        f"lead_id={lead_id or 'none'}", flush=True,
    )
    return selected


def find_active_conversations_by_enquiry_phone(
    enquiry_id, counterparty_phone, bubble_env="live",
):
    """Find active enquiry Conversations without requiring a Principal lookup."""
    enquiry_id = relationship_id(enquiry_id)
    phone = normalize_phone(counterparty_phone)
    if not enquiry_id or not phone:
        return []
    constraints = [
        {"key": "Enquiry", "constraint_type": "equals", "value": enquiry_id},
        {"key": "CounterParty Phone", "constraint_type": "equals", "value": phone},
        {"key": "Status", "constraint_type": "equals", "value": "Active"},
    ]
    matches = []
    for item in _records(
        get_bubble_base_url(bubble_env), "conversation", constraints
    ):
        visible_enquiry = relationship_id(item.get("Enquiry"))
        visible_phone = normalize_phone(item.get("CounterParty Phone"))
        visible_status = str(item.get("Status") or "").strip()
        if not item.get("_id"):
            continue
        if visible_enquiry and visible_enquiry != enquiry_id:
            continue
        if visible_phone and visible_phone != phone:
            continue
        if visible_status and visible_status != "Active":
            continue
        item.setdefault("Enquiry", enquiry_id)
        item.setdefault("CounterParty Phone", phone)
        item.setdefault("Status", "Active")
        matches.append(item)
    return matches


def create_conversation(
    principal_id, counterparty_phone, enquiry_id=None,
    counterparty_user_id=None, counterparty_name=None, counterparty_role=None,
    rentee_role=None, subject=None, bubble_env="live", lead_id=None,
    listing_id=None, side=None,
):
    principal_id = relationship_id(principal_id)
    phone = normalize_phone(counterparty_phone)
    enquiry_id = relationship_id(enquiry_id)
    lead_id = relationship_id(lead_id)
    counterparty_user_id = relationship_id(counterparty_user_id)
    if not phone:
        raise ValueError("Conversation requires CounterParty Phone.")
    if enquiry_id and not principal_id:
        raise ValueError("Enquiry Conversation requires Principal.")
    if not principal_id and not lead_id and not counterparty_user_id:
        raise ValueError(
            "General Conversation requires Principal, Lead, or Counterparty User."
        )
    payload = {
        "CounterParty Phone": phone,
        "Status": "Active",
    }
    if principal_id:
        payload["Principal"] = principal_id
    optional = {
        "Counterparty User": counterparty_user_id,
        "Counterparty Name": str(counterparty_name or "").strip() or None,
        "Enquiry": enquiry_id,
        "Lead": lead_id,
        "Listing": relationship_id(listing_id),
        "CounterParty Role": str(counterparty_role or "").strip() or None,
        "Rentee Role": str(rentee_role or "").strip() or None,
        "Subject": str(subject or "").strip() or None,
    }
    payload.update({key: value for key, value in optional.items() if value})
    print(
        f"[CONVERSATION] side={side or 'unspecified'} action=create_started "
        f"principal_id={principal_id} "
        f"enquiry_id={enquiry_id or 'none'}", flush=True,
    )
    conversation_id = _create(
        get_bubble_base_url(bubble_env), "conversation", payload
    )
    conversation = {"_id": conversation_id, **payload}
    print(
        f"[CONVERSATION] side={side or 'unspecified'} action=created "
        f"type={'enquiry' if enquiry_id else 'general'} "
        f"conversation_id={conversation_id} "
        f"principal_id={principal_id or 'none'} "
        f"lead_id={lead_id or 'none'} enquiry_id={enquiry_id or 'none'}",
        flush=True,
    )
    return conversation


def find_or_create_conversation(
    principal_id, counterparty_phone, enquiry_id=None,
    counterparty_user_id=None, counterparty_name=None, counterparty_role=None,
    rentee_role=None, subject=None, bubble_env="live", side=None, lead_id=None,
):
    enquiry_id = relationship_id(enquiry_id)
    lead_id = relationship_id(lead_id)
    listing_id = None
    if enquiry_id:
        enquiry = _get(
            get_bubble_base_url(bubble_env), "enquiry", enquiry_id
        )
        enquiry_lead_id = relationship_id(enquiry.get("Lead"))
        if enquiry_lead_id:
            lead_id = enquiry_lead_id
        listing_id = relationship_id(enquiry.get("Listing"))
    if principal_id:
        existing = find_active_conversation(
            principal_id, counterparty_phone, enquiry_id, bubble_env, side
        )
    elif not enquiry_id:
        existing = find_active_general_conversation(
            counterparty_phone, lead_id, counterparty_user_id, bubble_env, side
        )
    else:
        existing = None
    if existing:
        missing = {}
        if lead_id and not relationship_id(existing.get("Lead")):
            missing["Lead"] = lead_id
        if listing_id and not relationship_id(existing.get("Listing")):
            missing["Listing"] = listing_id
        if missing:
            _patch(
                get_bubble_base_url(bubble_env), "conversation", existing["_id"],
                missing,
            )
            existing.update(missing)
        return existing, False
    return create_conversation(
        principal_id, counterparty_phone, enquiry_id, counterparty_user_id,
        counterparty_name, counterparty_role, rentee_role, subject, bubble_env,
        lead_id, listing_id, side,
    ), True


def _utc_timestamp(now=None):
    value = now or datetime.datetime.now(datetime.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def update_conversation_last_inbound_at(conversation_id, bubble_env="live", now=None):
    payload = {"Last Inbound At": _utc_timestamp(now)}
    print(
        f"[CONVERSATION] action=update_started operation=last_inbound "
        f"conversation_id={conversation_id}", flush=True,
    )
    _patch(get_bubble_base_url(bubble_env), "conversation", conversation_id, payload)
    print(f"[CONVERSATION] message=inbound conversation_id={conversation_id}", flush=True)
    return payload


def update_conversation_last_outbound_at(conversation_id, bubble_env="live", now=None):
    payload = {"Last Outbound At": _utc_timestamp(now)}
    print(
        f"[CONVERSATION] action=update_started operation=last_outbound "
        f"conversation_id={conversation_id}", flush=True,
    )
    _patch(get_bubble_base_url(bubble_env), "conversation", conversation_id, payload)
    print(f"[CONVERSATION] message=outbound conversation_id={conversation_id}", flush=True)
    return payload


def get_conversation_previous_response_id(conversation):
    return str((conversation or {}).get("Previous Response ID") or "").strip() or None


def set_conversation_previous_response_id(
    conversation_id, response_id, bubble_env="live",
):
    payload = {"Previous Response ID": str(response_id or "").strip()}
    print(
        f"[CONVERSATION] action=update_started operation=previous_response "
        f"conversation_id={conversation_id}", flush=True,
    )
    _patch(get_bubble_base_url(bubble_env), "conversation", conversation_id, payload)
    return payload
