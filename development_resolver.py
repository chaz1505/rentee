"""Resolve and safely create Rentee Developments discovered during imports."""

from __future__ import annotations

import difflib
import json
import re
from typing import Any, Iterable

import app as rentee_app


FUZZY_THRESHOLD = 0.90
FUZZY_MARGIN = 0.08
VERIFICATION_CONFIDENCE = 0.90
DEVELOPMENT_VERIFIER_MAX_OUTPUT_TOKENS = 800


def _compact(value: Any) -> str:
    return " ".join(str(value or "").split())


def _record_name(record: dict) -> str:
    return _compact(next(
        (record.get(key) for key in ("name", "Name", "Condo name") if record.get(key)),
        "",
    ))


def _normalized_development(value: Any) -> str:
    text = _compact(value).casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _normalized_geo(value: Any) -> str:
    text = _compact(value).casefold()
    text = re.sub(r"\s*\([^)]*\)\s*", " ", text)
    previous = None
    while text != previous:
        previous = text
        text = re.sub(
            r"\s*,\s*(?:kuala\s+lumpur|malaysia)\s*$", "", text,
            flags=re.IGNORECASE,
        )
    text = text.replace("’", "'").replace("'", " ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _resolve_name(name: Any, records: Iterable[dict], kind: str,
                  normalizer) -> dict:
    raw = _compact(name)
    if not raw:
        return {"matched": False, "raw_name": raw}
    candidates = [(record, _record_name(record)) for record in records or []]
    candidates = [(record, canonical) for record, canonical in candidates
                  if record.get("_id") and canonical]
    stages = (
        ("exact", lambda canonical: canonical == raw),
        ("case_insensitive_exact", lambda canonical: canonical.casefold() == raw.casefold()),
        ("normalized_exact", lambda canonical: normalizer(canonical) == normalizer(raw)),
    )
    for method, predicate in stages:
        matches = [(record, canonical) for record, canonical in candidates
                   if predicate(canonical)]
        if len(matches) == 1:
            record, canonical = matches[0]
            print(f"[WHATSAPP IMPORT] {kind} {method} raw={raw!r} "
                  f"canonical={canonical!r} id={record['_id']}", flush=True)
            return {"matched": True, "id": str(record["_id"]), "name": canonical,
                    "method": method, "record": record}
        if len(matches) > 1:
            print(f"[WHATSAPP IMPORT] {kind} unresolved raw={raw!r}", flush=True)
            return {"matched": False, "raw_name": raw, "reason": "ambiguous"}
    key = normalizer(raw)
    scored = sorted((
        (difflib.SequenceMatcher(None, key, normalizer(canonical)).ratio(),
         record, canonical)
        for record, canonical in candidates
    ), key=lambda item: item[0], reverse=True)
    best = scored[0] if scored else None
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if best and best[0] >= FUZZY_THRESHOLD and best[0] - second_score >= FUZZY_MARGIN:
        score, record, canonical = best
        print(f"[WHATSAPP IMPORT] {kind} fuzzy raw={raw!r} canonical={canonical!r} "
              f"id={record['_id']} score={score:.3f}", flush=True)
        return {"matched": True, "id": str(record["_id"]), "name": canonical,
                "method": "fuzzy", "score": score, "record": record}
    print(f"[WHATSAPP IMPORT] {kind} unresolved raw={raw!r}", flush=True)
    return {"matched": False, "raw_name": raw,
            **({"reason": "ambiguous"} if best and best[0] >= FUZZY_THRESHOLD else {})}


def resolve_development_name(name, development_records):
    return _resolve_name(name, development_records, "development", _normalized_development)


def resolve_geo_name(name, geo_records):
    return _resolve_name(name, geo_records, "geo", _normalized_geo)


def _verification_error(raw, reason, value=None, output_preview=None):
    keys = sorted(str(key) for key in value) if isinstance(value, dict) else None
    suffix = f" response_keys={keys!r}" if keys is not None else ""
    if output_preview is not None:
        suffix += f" output_preview={_compact(output_preview)[:160]!r}"
    print(f"[DEVELOPMENT VERIFY] raw={raw!r} status=error "
          f"reason={reason!r}{suffix}", flush=True)
    return {"status": "error", "raw_name": raw, "canonical_name": None,
            "geo_name": None, "verification_url": None, "confidence": 0.0,
            "reason": reason}


def _extract_one_json_object(text):
    objects = []
    start = None
    depth = 0
    in_string = False
    escaped = False
    for index, character in enumerate(str(text or "")):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    candidate = json.loads(str(text)[start:index + 1])
                except (TypeError, json.JSONDecodeError):
                    pass
                else:
                    if isinstance(candidate, dict):
                        objects.append(candidate)
                start = None
    return objects[0] if len(objects) == 1 else None


def _verifier_response_value(response):
    parsed = getattr(response, "output_parsed", None)
    if hasattr(parsed, "model_dump"):
        parsed = parsed.model_dump()
    if isinstance(parsed, dict):
        return parsed
    output = str(getattr(response, "output_text", "") or "")
    try:
        value = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        value = _extract_one_json_object(output)
    if not isinstance(value, dict):
        raise ValueError("invalid_json")
    return value


def _verifier_response_log_details(response):
    status = getattr(response, "status", None)
    incomplete_details = getattr(response, "incomplete_details", None)
    if hasattr(incomplete_details, "model_dump"):
        incomplete_details = incomplete_details.model_dump()
    return (f" response_status={status!r} incomplete_details={incomplete_details!r}"
            f" output_preview={_compact(getattr(response, 'output_text', ''))[:160]!r}")


def verify_development_candidate(raw_name: str, context: dict,
                                 bubble_env: str = "live") -> dict:
    raw = _compact(raw_name)
    if not raw:
        return {"status": "not_found", "raw_name": raw, "canonical_name": None,
                "geo_name": None, "verification_url": None, "confidence": 0.0,
                "reason": "no_credible_property_match"}
    focused_context = {key: value for key, value in (context or {}).items()
                       if value not in (None, "", [], {})}
    print(f"[DEVELOPMENT VERIFY] raw={raw!r} action=web_model_check", flush=True)
    request = dict(
            model="gpt-5-mini", tools=[{"type": "web_search"}],
            input=(
                "Does this name refer to a real Malaysian residential property development, "
                "given the surrounding context? Use web search and answer the identity question "
                "directly; do not return or classify raw search results. Schools, workplaces, "
                "malls, landmarks, offices, and stations are disambiguation context only and "
                "must never be returned as a Development or residential Geo. Return verified "
                "with reason credible_match only for a confident, unambiguous real project, its "
                "canonical name, residential area, and one strong source URL. canonical_name must "
                "be the clean official property name suitable for storage/display, without aliases "
                "or explanatory text in brackets or parentheses; for example return 'Residensi "
                "Sefina', not \"Residensi Sefina (Residensi Sefina Mont' Kiara)\", and return "
                "\"Mont' Kiara Astana\", not \"Mont' Kiara Astana (MK Astana)\". Return ambiguous "
                "with reason multiple_plausible_candidates when necessary, otherwise not_found "
                "with reason no_credible_property_match. Do not invent a Development.\n\n"
                f"CANDIDATE NAME:\n{raw}\n\nWHATSAPP IMPORT CONTEXT:\n"
                f"{json.dumps(focused_context, ensure_ascii=False)}"
            ),
            reasoning={"effort": "low"},
            max_output_tokens=DEVELOPMENT_VERIFIER_MAX_OUTPUT_TOKENS, timeout=20,
            text={"format": {"type": "json_schema",
                "name": "development_web_verification", "strict": True,
                "schema": {
                    "type": "object", "properties": {
                        "status": {"type": "string", "enum": [
                            "verified", "ambiguous", "not_found"]},
                        "canonical_name": {"type": ["string", "null"]},
                        "geo_name": {"type": ["string", "null"]},
                        "verification_url": {"type": ["string", "null"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string", "enum": [
                            "credible_match", "multiple_plausible_candidates",
                            "no_credible_property_match"]},
                    },
                    "required": ["status", "canonical_name", "geo_name",
                                 "verification_url", "confidence", "reason"],
                    "additionalProperties": False,
                }},},
        )
    try:
        response = rentee_app.client.responses.create(**request)
    except Exception as error:
        print(f"[DEVELOPMENT VERIFY] raw={raw!r} status=error "
              f"reason='web_tool_error' error={type(error).__name__}", flush=True)
        return _verification_error(raw, "web_tool_error")
    try:
        value = _verifier_response_value(response)
    except ValueError:
        print(f"[DEVELOPMENT VERIFY] raw={raw!r} status=retry "
              f"reason='invalid_json'"
              f"{_verifier_response_log_details(response)}", flush=True)
        retry_request = dict(request)
        retry_request["input"] = (
            request["input"]
            + "\n\nRetry after invalid JSON. Return ONLY one complete JSON object matching "
              "the required schema, with no prose or markdown."
        )
        try:
            response = rentee_app.client.responses.create(**retry_request)
        except Exception as error:
            print(f"[DEVELOPMENT VERIFY] raw={raw!r} status=error "
                  f"reason='web_tool_error' error={type(error).__name__}", flush=True)
            return _verification_error(raw, "web_tool_error")
        try:
            value = _verifier_response_value(response)
        except ValueError:
            print(f"[DEVELOPMENT VERIFY] raw={raw!r} status=retry_failed "
                  f"reason='invalid_json'"
                  f"{_verifier_response_log_details(response)}", flush=True)
            return _verification_error(
                raw, "invalid_json",
                output_preview=getattr(response, "output_text", ""),
            )
        print(f"[DEVELOPMENT VERIFY] raw={raw!r} status=retry_succeeded "
              f"result='valid_json'", flush=True)
    if not isinstance(value, dict):
        return _verification_error(raw, "schema_validation_failed")
    if "status" not in value:
        return _verification_error(raw, "missing_status", value)
    status = value.get("status")
    if status not in {"verified", "ambiguous", "not_found"}:
        return _verification_error(raw, "invalid_status", value)
    for field in ("canonical_name", "geo_name", "verification_url", "confidence", "reason"):
        if field not in value:
            return _verification_error(raw, f"missing_{field}", value)
    confidence, reason = value["confidence"], value["reason"]
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1):
        return _verification_error(raw, "invalid_confidence", value)
    allowed_reasons = {"credible_match", "multiple_plausible_candidates",
                       "no_credible_property_match"}
    if reason not in allowed_reasons:
        return _verification_error(raw, "schema_validation_failed", value)
    if status == "verified":
        for field in ("canonical_name", "geo_name", "verification_url"):
            if not _compact(value[field]):
                return _verification_error(raw, f"missing_{field}", value)
        if not str(value["verification_url"]).startswith(("https://", "http://")):
            return _verification_error(raw, "schema_validation_failed", value)
        if reason != "credible_match":
            return _verification_error(raw, "schema_validation_failed", value)
        if confidence < VERIFICATION_CONFIDENCE:
            result = {"status": "not_found", "raw_name": raw,
                      "canonical_name": None, "geo_name": None,
                      "verification_url": None, "confidence": float(confidence),
                      "reason": "verification_confidence_below_threshold"}
        else:
            result = {"status": "verified", "raw_name": raw,
                      "canonical_name": _compact(value["canonical_name"]),
                      "geo_name": _compact(value["geo_name"]),
                      "verification_url": value["verification_url"],
                      "confidence": float(confidence), "reason": reason}
    elif status == "ambiguous" and reason == "multiple_plausible_candidates":
        result = {"status": "ambiguous", "raw_name": raw,
                  "canonical_name": None, "geo_name": None,
                  "verification_url": None, "confidence": float(confidence),
                  "reason": reason}
    elif status == "not_found" and reason == "no_credible_property_match":
        result = {"status": "not_found", "raw_name": raw,
                  "canonical_name": None, "geo_name": None,
                  "verification_url": None, "confidence": float(confidence),
                  "reason": reason}
    else:
        return _verification_error(raw, "schema_validation_failed", value)
    print(f"[DEVELOPMENT VERIFY] raw={raw!r} status={result['status']} "
          f"reason={result['reason']!r}", flush=True)
    return result


def _fresh_development_records(bubble_env):
    return list(rentee_app._bubble_records(
        rentee_app.get_bubble_base_url(bubble_env), "condo"
    ))


def create_verified_development(canonical_name, resolved_geo, verification_url,
                                bubble_env="live", *, development_records=None):
    canonical = _compact(canonical_name)
    if not canonical or not resolved_geo or not resolved_geo.get("matched"):
        return None
    records = (list(development_records) if development_records is not None
               else _fresh_development_records(bubble_env))
    existing = resolve_development_name(canonical, records)
    if existing.get("matched"):
        return existing
    payload = {"name": canonical, "Geo": resolved_geo["id"],
               "verification_status": "Verified", "source": "WhatsApp Import",
               "verification_url": verification_url}
    try:
        development_id = rentee_app._bubble_create(
            rentee_app.get_bubble_base_url(bubble_env), "condo", payload
        )
    except Exception as error:
        try:
            raced = resolve_development_name(canonical, _fresh_development_records(bubble_env))
        except Exception:
            raced = {"matched": False}
        if raced.get("matched"):
            return raced
        print(f"[DEVELOPMENT CREATE] canonical={canonical!r} action=failed "
              f"error={type(error).__name__}", flush=True)
        return None
    record = {"_id": development_id, **payload}
    print(f"[DEVELOPMENT CREATE] canonical={canonical!r} "
          f"action=created id={development_id}", flush=True)
    return {"matched": True, "id": str(development_id), "name": canonical,
            "method": "web_verified_created", "record": record, "created": True}


def _geo_details(record, geo_records):
    value = record.get("Geo") if record else None
    values = value if isinstance(value, list) else [value]
    geo_ids = [str(item.get("_id") if isinstance(item, dict) else item)
               for item in values if item]
    for geo in geo_records or []:
        if str(geo.get("_id")) in geo_ids:
            return str(geo["_id"]), _record_name(geo)
    return (geo_ids[0], None) if geo_ids else (None, None)


def resolve_or_create_development(raw_name: str, context: dict,
                                  bubble_env: str = "live", *,
                                  development_records=None,
                                  geo_records=None,
                                  geo_verifier=None) -> dict:
    """Resolve an existing Development or safely verify and create it."""
    if development_records is None or geo_records is None:
        records = rentee_app._property_entity_records(bubble_env)
        development_records = (records["condo"] if development_records is None
                               else development_records)
        geo_records = records["geo"] if geo_records is None else geo_records
    development_records = list(development_records or [])
    geo_records = list(geo_records or [])
    raw = _compact(raw_name)
    existing = resolve_development_name(raw, development_records)
    if existing.get("matched"):
        geo_id, geo_name = _geo_details(existing["record"], geo_records)
        return {"status": "resolved", "action": "existing", "raw_name": raw,
                "canonical_name": existing["name"], "development_id": existing["id"],
                "geo_id": geo_id, "geo_name": geo_name, "record": existing["record"],
                "method": existing["method"]}
    verification = verify_development_candidate(raw, context, bubble_env)
    if verification.get("status") != "verified":
        return {"status": verification.get("status", "error"), "action": "none",
                "raw_name": raw,
                "reason": verification.get("reason", "schema_validation_failed")}
    resolved_geo = resolve_geo_name(verification["geo_name"], geo_records)
    if not resolved_geo.get("matched") and geo_verifier is not None:
        try:
            verified_geos = geo_verifier(
                verification["geo_name"], geo_records, context, single=True,
                bubble_env=bubble_env,
            )
        except Exception as error:
            print(f"[DEVELOPMENT CREATE] canonical={verification['canonical_name']!r} "
                  f"action=geo_fallback_failed error={type(error).__name__}",
                  flush=True)
            verified_geos = []
        if len(verified_geos) == 1 and verified_geos[0].get("matched"):
            resolved_geo = verified_geos[0]
    if not resolved_geo.get("matched"):
        print(f"[DEVELOPMENT CREATE] canonical={verification['canonical_name']!r} "
              f"action=skipped reason=geo_unresolved "
              f"geo_candidate={verification['geo_name']!r}", flush=True)
        return {"status": "not_found", "action": "none", "raw_name": raw,
                "reason": "geo_unresolved"}
    canonical = resolve_development_name(
        verification["canonical_name"], development_records
    )
    if canonical.get("matched"):
        return {"status": "resolved", "action": "existing", "raw_name": raw,
                "canonical_name": canonical["name"],
                "development_id": canonical["id"], "geo_id": resolved_geo["id"],
                "geo_name": resolved_geo["name"], "record": canonical["record"],
                "geo_record": resolved_geo.get("record"),
                "method": canonical["method"],
                "verification_url": verification["verification_url"]}
    created = create_verified_development(
        verification["canonical_name"], resolved_geo,
        verification["verification_url"], bubble_env,
    )
    if not created or not created.get("matched"):
        return {"status": "error", "action": "none", "raw_name": raw,
                "reason": "development_create_failed"}
    return {"status": "resolved",
            "action": "created" if created.get("created") else "existing",
            "raw_name": raw, "canonical_name": created["name"],
            "development_id": created["id"], "geo_id": resolved_geo["id"],
            "geo_name": resolved_geo["name"], "record": created["record"],
            "geo_record": resolved_geo.get("record"),
            "method": created.get("method"),
            "verification_url": verification["verification_url"]}
