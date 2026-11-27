"""Best-effort, transport-level WhatsApp inbox persistence.

This module deliberately has no dependency on ``app.py``.  The main service passes
its Bubble helpers in, which keeps inbox failures isolated from message delivery.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import re


STATUS_RANK = {"submitted": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}
MESSAGE_TYPES = {"text", "image", "template", "audio", "document", "video", "other"}


def enabled():
    return os.getenv("WHATSAPP_INBOX_ENABLED", "false").strip().casefold() in {"1", "true", "yes", "on"}


def log_failure(action, error):
    print(f"[WHATSAPP INBOX] action={action} error={type(error).__name__}", flush=True)


def verify_signature(raw_body, signature, app_secret=None):
    """Validate Meta's X-Hub-Signature-256 value using a timing-safe compare."""
    secret = app_secret or os.getenv("WHATSAPP_APP_SECRET")
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


def _timestamp(value=None):
    if value not in (None, ""):
        try:
            return dt.datetime.fromtimestamp(int(value), dt.timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _content(item):
    kind = str(item.get("type") or "other").casefold()
    data = item.get(kind) if isinstance(item.get(kind), dict) else {}
    if kind == "text":
        return str(data.get("body") or "")
    if kind == "image":
        return str(data.get("caption") or "")
    name = data.get("filename") or data.get("mime_type") or data.get("id")
    return f"[{kind.title()}{': ' + str(name) if name else ''}]"


def inbound_events(payload):
    events = []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return events
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for item in value.get("messages") or []:
                if item.get("id") and item.get("from"):
                    kind = str(item.get("type") or "other").casefold()
                    events.append({
                        "phone": item["from"], "direction": "Inbound",
                        "messageContent": _content(item),
                        "messageType": kind if kind in MESSAGE_TYPES else "other",
                        "whatsappMessageId": item["id"],
                        "whatsapp_status": "received",
                        "whatsapp_timestamp": _timestamp(item.get("timestamp")),
                        "inbox_only": True,
                    })
    return events


def status_events(payload):
    events = []
    if not isinstance(payload, dict):
        return events
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for item in (change.get("value") or {}).get("statuses") or []:
                status = str(item.get("status") or "").casefold()
                if item.get("id") and status in STATUS_RANK:
                    errors = item.get("errors") or []
                    error = errors[0] if errors and isinstance(errors[0], dict) else {}
                    description = str(error.get("title") or error.get("message") or error.get("details") or "")
                    description = re.sub(r"[\r\n\t]+", " ", description)[:240]
                    code = str(error.get("code") or "").strip()
                    events.append({"id": item["id"], "status": status,
                                   "phone": item.get("recipient_id"),
                                   "timestamp": _timestamp(item.get("timestamp")),
                                   "error": " - ".join(filter(None, (code, description)))})
    return events


def upsert_transport(event, *, records, create, patch, normalize_phone):
    """Create or enrich a Message by transport ID; safe to call on retries."""
    meta_id = str(event.get("whatsappMessageId") or "").strip()
    if not meta_id:
        return None
    constraints = [{"key": "whatsappMessageId", "constraint_type": "equals", "value": meta_id}]
    existing = next(iter(records("message", constraints)), None)
    payload = dict(event)
    payload["phone"] = normalize_phone(payload.get("phone"))
    if existing and existing.get("_id"):
        update = {key: value for key, value in payload.items()
                  if value not in (None, "") and not existing.get(key)}
        if update:
            patch(existing["_id"], update)
        return existing["_id"]
    return create("message", payload)


def apply_status(event, *, records, patch, create=None):
    constraints = [{"key": "whatsappMessageId", "constraint_type": "equals", "value": event["id"]}]
    existing = next(iter(records("message", constraints)), None)
    if not existing or not existing.get("_id"):
        # Persist a durable placeholder when possible; the later send capture
        # enriches the same row by Meta ID rather than creating a duplicate.
        if create is not None:
            payload = {
                "phone": str(event.get("phone") or ""),
                "direction": "Outbound", "own_Sent?": "No",
                "messageContent": "", "messageType": "other",
                "whatsappMessageId": event["id"], "inbox_only": True,
                "whatsapp_status": event["status"],
                "whatsapp_timestamp": event["timestamp"],
            }
            if event.get("error"):
                payload["whatsapp_error"] = event["error"]
            create("message", payload)
            return True
        # Fall back to a bounded spool for callers without create capability.
        _PENDING_STATUS[event["id"]] = event
        if len(_PENDING_STATUS) > 1000:
            _PENDING_STATUS.pop(next(iter(_PENDING_STATUS)))
        return False
    old = str(existing.get("whatsapp_status") or "").casefold()
    new = event["status"]
    if old == "failed" or (old == "read" and new == "failed"):
        return True
    if old and new != "failed" and STATUS_RANK.get(new, -1) < STATUS_RANK.get(old, -1):
        return True
    values = {"whatsapp_status": new, "whatsapp_timestamp": event["timestamp"]}
    if event.get("error"):
        values["whatsapp_error"] = event["error"]
    patch(existing["_id"], values)
    return True


def pending_status(meta_id):
    return _PENDING_STATUS.pop(str(meta_id or ""), None)


def template_display(name, parameters=None, button_parameters=None):
    values = [str(value) for value in (parameters or [])]
    buttons = [str(value) for value in (button_parameters or [])]
    details = []
    if values:
        details.append("parameters: " + ", ".join(values))
    if buttons:
        details.append("buttons: " + ", ".join(buttons))
    return f"[Template: {name}]" + ("\n" + "\n".join(details) if details else "")


_PENDING_STATUS = {}
