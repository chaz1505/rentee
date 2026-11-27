"""Standalone read-only Rentee operations dashboard."""

from __future__ import annotations

import datetime as dt
import base64
import hmac
import json
import os
import secrets
import threading
import time
import re
from collections import defaultdict, deque
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from bubble_api import BubbleAPI, BubbleAPIError, TTLCache, parallel_counts

KL = ZoneInfo("Asia/Kuala_Lumpur")
VALID_RANGES = {"today", "7d", "30d", "all"}
VALID_MATCH_FILTERS = {"all", "sent", "not_sent"}
SITE_URL = "https://www.rentee.asia"
INBOX_PAGE_SIZE = 30
INBOX_BUBBLE_PAGE = 100
INBOX_CACHE_ROWS = 600
MATCH_PAGE_SIZE = 20


def _normal_phone(value):
    return "".join(character for character in str(value or "") if character.isdigit())


def _redact_inbox_content(value):
    text = str(value or "")
    text = re.sub(r"(?i)(\b(?:otp|code|token)\s*[:=-]?\s*)\d{4,8}\b", r"\1[REDACTED]", text)
    text = re.sub(r"https?://[^\s]*(?:login|magic|auth|token)[^\s]*", "[REDACTED LOGIN LINK]", text, flags=re.I)
    return text


def _message_time(row):
    return row.get("whatsapp_timestamp") or row.get("Created Date") or row.get("created_date")


def _message_datetime(row):
    value = _datetime(_message_time(row))
    if value is None:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _message_key(row):
    """A deterministic key for mixed timestamps, including timestamp ties."""
    return (_message_datetime(row), str(row.get("_id") or ""))


def _cursor(value):
    if not value:
        return 0
    try:
        raw = base64.urlsafe_b64decode(value.encode() + b"=" * (-len(value) % 4))
        decoded = json.loads(raw)
        offset = int(decoded["offset"])
        if offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise ValueError("Invalid pagination cursor")


def _next_cursor(offset):
    raw = json.dumps({"offset": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _message_cursor(row):
    raw = json.dumps({
        "time": _message_datetime(row).isoformat(),
        "id": str(row.get("_id") or ""),
    }, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _message_boundary(value):
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value.encode() + b"=" * (-len(value) % 4))
        decoded = json.loads(raw)
        stamp = _datetime(decoded["time"])
        if stamp is None:
            raise ValueError
        return (stamp.astimezone(dt.timezone.utc), str(decoded["id"]))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise ValueError("Invalid pagination cursor")


def _phone_constraints(phone):
    return [{"key": "phone", "constraint_type": "equals", "value": _normal_phone(phone)}]


def _phone_constraint_sets(phone):
    """Exact current format first, plus a bounded legacy-format lookup."""
    normal = _normal_phone(phone)
    return [
        _phone_constraints(normal),
        [{"key": "phone", "constraint_type": "text contains", "value": normal[-4:]}],
    ]


def _bounded_message_rows(client, constraints, cap=INBOX_CACHE_ROWS):
    """Merge Bubble's two sortable timestamp streams without an unbounded scan."""
    timestamped = client.records(
        "message", constraints, limit=cap,
        sort_field="whatsapp_timestamp", descending=True,
    )
    created = client.records(
        "message", constraints, limit=cap,
        sort_field="Created Date", descending=True,
    )
    rows = {str(row.get("_id") or id(row)): row for row in timestamped + created}
    return sorted(rows.values(), key=_message_key, reverse=True)[:cap]


def _bounded_phone_message_rows(client, phone, cap=INBOX_CACHE_ROWS):
    rows = {}
    for constraints in _phone_constraint_sets(phone):
        for row in _bounded_message_rows(client, constraints, cap):
            if _normal_phone(row.get("phone")) == _normal_phone(phone):
                rows[str(row.get("_id") or id(row))] = row
    return sorted(rows.values(), key=_message_key, reverse=True)[:cap]


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _datetime(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _phone_valid(value):
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return 8 <= len(digits) <= 15


def _intended_recipient(match, lead, listing):
    """Use persisted recipient, or conservatively infer which record triggered the match."""
    if match.get("alert_recipient"):
        return match["alert_recipient"]
    lead_created = _datetime(lead.get("Created Date"))
    listing_created = _datetime(listing.get("Created Date"))
    match_created = _datetime(match.get("Created Date"))
    if not all((lead_created, listing_created, match_created)):
        return None
    # Production creates matches immediately after importing the new side. Only
    # infer when exactly one side is both newer and close to match creation.
    lead_age = (match_created - lead_created).total_seconds()
    listing_age = (match_created - listing_created).total_seconds()
    lead_is_trigger = 0 <= lead_age <= 300 and lead_created > listing_created
    listing_is_trigger = 0 <= listing_age <= 300 and listing_created > lead_created
    if lead_is_trigger != listing_is_trigger:
        return match.get("listing_owner") if lead_is_trigger else match.get("lead_owner")
    return None


def _unsent_reason(match, recipient, threshold):
    score = _number(match.get("match_score"))
    if score is None:
        return "Match not scored"
    if score < threshold:
        return f"Below minimum alert score ({threshold:g})"
    lead_owner, listing_owner = match.get("lead_owner"), match.get("listing_owner")
    if lead_owner and lead_owner == listing_owner:
        return "Same agent owns lead and listing"
    if recipient is not None:
        if recipient.get("alerts") is not True:
            return "Agent alerts disabled"
        if not _phone_valid(recipient.get("phone")):
            return "Missing or invalid phone number"
    return "Reason not recorded"


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("DASHBOARD_SECRET_KEY"),
        DASHBOARD_PASSWORD=os.getenv("DASHBOARD_PASSWORD"),
        BUBBLE_API_BASE_URL=os.getenv("BUBBLE_API_BASE_URL"),
        BUBBLE_API_TOKEN=os.getenv("BUBBLE_API_TOKEN"),
        MATCH_ALERT_MIN_SCORE=os.getenv("MATCH_ALERT_MIN_SCORE", "80"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=12),
    )
    if config:
        app.config.update(config)

    cache = TTLCache(60)
    inbox_cache = TTLCache(10)
    attempts = defaultdict(deque)
    attempt_lock = threading.Lock()

    def configured(*keys):
        return all(app.config.get(key) for key in keys)

    def api():
        if not configured("BUBBLE_API_BASE_URL", "BUBBLE_API_TOKEN"):
            raise BubbleAPIError("Dashboard data service is not configured")
        return BubbleAPI(
            app.config["BUBBLE_API_BASE_URL"], app.config["BUBBLE_API_TOKEN"]
        )

    def csrf_token():
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = csrf_token

    def csrf_valid():
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        expected = session.get("csrf_token", "")
        return bool(supplied and expected and hmac.compare_digest(supplied, expected))

    def authenticated():
        return session.get("authenticated") is True

    def require_api_auth(view):
        def wrapped(*args, **kwargs):
            if not authenticated():
                return jsonify({"error": "Authentication required"}), 401
            return view(*args, **kwargs)
        wrapped.__name__ = view.__name__
        return wrapped

    def date_constraints(period):
        if period == "all":
            return []
        now = dt.datetime.now(KL)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "7d":
            start -= dt.timedelta(days=6)
        elif period == "30d":
            start -= dt.timedelta(days=29)
        start -= dt.timedelta(milliseconds=1)
        return [{"key": "Created Date", "constraint_type": "greater than", "value": start.isoformat()}]

    def alert_date_constraints(period):
        constraints = [{"key": "alert_sent", "constraint_type": "equals", "value": True}]
        for item in date_constraints(period):
            constraints.append(dict(item, key="alert_sent_at"))
        return constraints

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not configured("SECRET_KEY", "DASHBOARD_PASSWORD"):
            return render_template(
                "login.html", error="Dashboard authentication is not configured.",
                auth_configured=False,
            ), 503
        error = None
        if request.method == "POST":
            if not csrf_valid():
                return render_template("login.html", error="Your session expired. Please try again."), 400
            key = request.remote_addr or "unknown"
            now = time.monotonic()
            with attempt_lock:
                recent = attempts[key]
                while recent and now - recent[0] > 900:
                    recent.popleft()
                limited = len(recent) >= 5
            if limited:
                return render_template("login.html", error="Too many attempts. Try again later."), 429
            password = request.form.get("password", "")
            if hmac.compare_digest(password, str(app.config["DASHBOARD_PASSWORD"])):
                session.clear()
                session["authenticated"] = True
                session.permanent = True
                csrf_token()
                with attempt_lock:
                    attempts.pop(key, None)
                return redirect(url_for("dashboard"))
            with attempt_lock:
                attempts[key].append(now)
            error = "Incorrect password."
        return render_template("login.html", error=error)

    @app.post("/logout")
    def logout():
        if not authenticated():
            return redirect(url_for("login"))
        if not csrf_valid():
            return "Invalid CSRF token", 400
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def dashboard():
        if not authenticated():
            return redirect(url_for("login"))
        return render_template("dashboard.html")

    @app.get("/inbox")
    def inbox():
        if not authenticated():
            return redirect(url_for("login"))
        return render_template("inbox.html")

    @app.get("/api/inbox/conversations")
    @require_api_auth
    def inbox_conversations():
        try:
            offset = _cursor(request.args.get("cursor"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        query = request.args.get("search", "").strip().lower()[:100]
        try:
            client = api()
            def load_conversations():
                latest = {}
                for row in _bounded_message_rows(client, [], INBOX_BUBBLE_PAGE):
                    phone = _normal_phone(row.get("phone"))
                    if phone and (phone not in latest or _message_key(row) > _message_key(latest[phone])):
                        latest[phone] = row

                # The user lookup is bounded and shared across Inbox requests.
                users = inbox_cache.get_or_create("inbox-users", lambda: client.records(
                    "user", [], limit=INBOX_CACHE_ROWS,
                    sort_field="Modified Date", descending=True,
                ))
                names = {}
                for user in users:
                    phone = _normal_phone(user.get("phone") or user.get("Phone"))
                    if phone:
                        names[phone] = user.get("name") or user.get("Name")
                output = [{
                    "phone": phone, "name": names.get(phone) or phone,
                    "preview": _redact_inbox_content(row.get("messageContent"))[:120],
                    "timestamp": _message_time(row), "direction": row.get("direction"),
                } for phone, row in latest.items()]
                return sorted(output, key=lambda item: (
                    _message_datetime({"whatsapp_timestamp": item.get("timestamp")}),
                    item["phone"],
                ), reverse=True)

            output = inbox_cache.get_or_create("inbox-conversations", load_conversations)
            if query:
                local = [item for item in output if query in
                         f'{item["name"]} {item["phone"]}'.lower()]

                def remote_search():
                    rows, names = [], {}
                    digits = _normal_phone(query)
                    if len(digits) >= 4:
                        if _phone_valid(digits):
                            rows.extend(_bounded_phone_message_rows(
                                client, digits, INBOX_BUBBLE_PAGE
                            ))
                        else:
                            rows.extend(_bounded_message_rows(client, [{
                                "key": "phone", "constraint_type": "text contains",
                                "value": digits,
                            }], INBOX_BUBBLE_PAGE))
                    users = client.records("user", [{
                        "key": "name", "constraint_type": "text contains", "value": query,
                    }], limit=INBOX_PAGE_SIZE,
                                      sort_field="Modified Date", descending=True)
                    for user in users:
                        user_phone = _normal_phone(user.get("phone") or user.get("Phone"))
                        if not user_phone:
                            continue
                        names[user_phone] = user.get("name") or user.get("Name") or user_phone
                        rows.extend(_bounded_phone_message_rows(
                            client, user_phone, INBOX_PAGE_SIZE
                        ))
                    latest = {}
                    for row in rows:
                        row_phone = _normal_phone(row.get("phone"))
                        if row_phone and (not digits or digits in row_phone or row_phone in names):
                            if row_phone not in latest or _message_key(row) > _message_key(latest[row_phone]):
                                latest[row_phone] = row
                    return [{
                        "phone": item_phone, "name": names.get(item_phone) or item_phone,
                        "preview": _redact_inbox_content(row.get("messageContent"))[:120],
                        "timestamp": _message_time(row), "direction": row.get("direction"),
                    } for item_phone, row in sorted(
                        latest.items(), key=lambda item: _message_key(item[1]), reverse=True
                    )]

                remote = inbox_cache.get_or_create(f"inbox-search:{query}", remote_search)
                combined = {item["phone"]: item for item in remote}
                combined.update({item["phone"]: item for item in local})
                output = sorted(combined.values(), key=lambda item: (
                    _message_datetime({"whatsapp_timestamp": item.get("timestamp")}),
                    item["phone"],
                ), reverse=True)
            page = output[offset:offset + INBOX_PAGE_SIZE]
            next_offset = offset + len(page)
            has_more = next_offset < len(output)
            return jsonify({
                "conversations": page, "has_more": has_more,
                "next_cursor": _next_cursor(next_offset) if has_more else None,
            })
        except BubbleAPIError as exc:
            return jsonify({"error": str(exc)}), 503

    @app.get("/api/inbox/messages")
    @require_api_auth
    def inbox_messages():
        phone = _normal_phone(request.args.get("phone"))
        if not _phone_valid(phone):
            return jsonify({"error": "Invalid phone number"}), 400
        try:
            boundary = _message_boundary(request.args.get("cursor"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        since = _datetime(request.args.get("since"))
        try:
            client = api()
            state = inbox_cache.get_or_create(f"inbox-messages:{phone}", lambda: {
                "rows": {},
                "streams": {
                    (source, field): {"offset": 0, "more": True}
                    for source in range(len(_phone_constraint_sets(phone)))
                    for field in ("whatsapp_timestamp", "Created Date")
                },
            })

            def fetch_batch():
                changed = False
                constraint_sets = _phone_constraint_sets(phone)
                for (source, field), stream in state["streams"].items():
                    if not stream["more"]:
                        continue
                    found, more = client.bounded_page(
                        "message", constraint_sets[source],
                        cursor=stream["offset"], limit=INBOX_PAGE_SIZE,
                        sort_field=field, descending=True,
                    )
                    stream["offset"] += len(found)
                    stream["more"] = more
                    for row in found:
                        if _normal_phone(row.get("phone")) == phone:
                            state["rows"][str(row.get("_id") or id(row))] = row
                            changed = True
                return changed

            if not state["rows"]:
                fetch_batch()
            rows = sorted(state["rows"].values(), key=_message_key, reverse=True)
            if not since:
                eligible = [row for row in rows if boundary is None or _message_key(row) < boundary]
                while len(eligible) < INBOX_PAGE_SIZE and any(
                    stream["more"] for stream in state["streams"].values()
                ):
                    if not fetch_batch():
                        break
                    rows = sorted(state["rows"].values(), key=_message_key, reverse=True)
                    eligible = [row for row in rows if boundary is None or _message_key(row) < boundary]
            if since:
                selected = [row for row in rows if _message_datetime(row) >= since]
                # Refresh responses are chronological and include equal timestamps;
                # the browser de-duplicates by immutable Bubble id.
                selected.sort(key=_message_key)
                has_more = False
                next_value = None
            else:
                eligible = [row for row in rows if boundary is None or _message_key(row) < boundary]
                selected = eligible[:INBOX_PAGE_SIZE]
                has_more = len(eligible) > len(selected) or any(
                    stream["more"] for stream in state["streams"].values()
                )
                next_value = _message_cursor(selected[-1]) if has_more and selected else None
                selected = list(reversed(selected))
            messages = [{
                "id": row.get("_id"), "phone": phone,
                "direction": row.get("direction"),
                "content": _redact_inbox_content(row.get("messageContent")),
                "type": row.get("messageType") or "text",
                "image_url": row.get("image_URL"),
                "timestamp": _message_time(row),
                "status": row.get("whatsapp_status"),
                "error": _redact_inbox_content(row.get("whatsapp_error")),
            } for row in selected]
            return jsonify({
                "phone": phone, "messages": messages, "has_more": has_more,
                "next_cursor": next_value,
            })
        except BubbleAPIError as exc:
            return jsonify({"error": str(exc)}), 503

    @app.get("/api/stats")
    @require_api_auth
    def stats():
        period = request.args.get("range", "today")
        if period not in VALID_RANGES:
            return jsonify({"error": "Invalid date range"}), 400

        def load():
            client = api()
            created = date_constraints(period)
            values = parallel_counts(client, {
                "leads": ("lead", created),
                "listings": ("listing", created),
                "matches": ("match", created),
                "alerts_sent": ("match", alert_date_constraints(period)),
                # The schema has no dependable agent discriminator: imported
                # agents are ordinary User records, so these are user metrics.
                "total_users": ("user", []),
                "users_alerts_enabled": ("user", [{"key": "alerts", "constraint_type": "equals", "value": True}]),
            })
            return {"range": period, **values}
        try:
            return jsonify(cache.get_or_create(f"stats:{period}", load))
        except BubbleAPIError as exc:
            return jsonify({"error": str(exc)}), 503

    @app.get("/api/alerts")
    @app.get("/api/matches")
    @require_api_auth
    def alerts():
        period = request.args.get("range", "today")
        if period not in VALID_RANGES:
            return jsonify({"error": "Invalid date range"}), 400
        status_filter = request.args.get("status", "all")
        if status_filter not in VALID_MATCH_FILTERS:
            return jsonify({"error": "Invalid match status"}), 400
        try:
            page = int(request.args.get("page", "1"))
            if page < 1:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid page number"}), 400
        try:
            threshold = float(app.config.get("MATCH_ALERT_MIN_SCORE", 80))
        except (TypeError, ValueError):
            threshold = 80.0

        def load():
            client = api()
            constraints = date_constraints(period)
            if status_filter == "sent":
                constraints = constraints + [{"key": "alert_sent", "constraint_type": "equals", "value": True}]
            offset = (page - 1) * MATCH_PAGE_SIZE
            if status_filter == "not_sent":
                # Bubble does not treat an empty boolean as false. Query both
                # states explicitly, then merge only the bounded prefix needed
                # to produce this page in newest-first order.
                false_constraints = constraints + [{
                    "key": "alert_sent", "constraint_type": "equals", "value": False,
                }]
                empty_constraints = constraints + [{
                    "key": "alert_sent", "constraint_type": "is_empty",
                }]
                false_total = client.count("match", false_constraints)
                empty_total = client.count("match", empty_constraints)
                total_matches = false_total + empty_total
                prefix_size = min(offset + MATCH_PAGE_SIZE, total_matches)
                false_rows = client.records(
                    "match", false_constraints, limit=min(prefix_size, false_total),
                    sort_field="Created Date", descending=True,
                )
                empty_rows = client.records(
                    "match", empty_constraints, limit=min(prefix_size, empty_total),
                    sort_field="Created Date", descending=True,
                )
                combined = false_rows + empty_rows
                combined.sort(key=lambda row: (
                    _datetime(row.get("Created Date")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                    str(row.get("_id") or ""),
                ), reverse=True)
                matches = combined[offset:offset + MATCH_PAGE_SIZE]
            else:
                total_matches = client.count("match", constraints)
                matches = [] if offset >= total_matches else client.records(
                    "match", constraints, limit=MATCH_PAGE_SIZE,
                    cursor=offset, sort_field="Created Date", descending=True,
                )
            user_ids, lead_ids, listing_ids = set(), set(), set()
            for match in matches:
                user_ids.update(filter(None, [match.get("alert_recipient"), match.get("lead_owner"), match.get("listing_owner")]))
                lead_ids.add(match.get("lead")); listing_ids.add(match.get("listing"))
            users = {r.get("_id"): r for r in client.records_by_ids("user", user_ids)}
            leads = {r.get("_id"): r for r in client.records_by_ids("lead", lead_ids)}
            listings = {r.get("_id"): r for r in client.records_by_ids("listing", listing_ids)}
            development_ids = {r.get("development") for r in listings.values() if r.get("development")}
            developments = {r.get("_id"): r for r in client.records_by_ids("development", development_ids)}
            output = []
            for match in matches:
                listing = listings.get(match.get("listing"), {})
                lead = leads.get(match.get("lead"), {})
                recipient_id = _intended_recipient(match, lead, listing)
                recipient_record = users.get(recipient_id) if recipient_id else None
                development = developments.get(listing.get("development"), {})
                area = development.get("name") or development.get("Name")
                if not area:
                    geo = lead.get("Geo") or listing.get("Geo")
                    area = geo if isinstance(geo, str) and not geo.startswith("1") else "Not specified"
                output.append({
                    "id": match.get("_id"),
                    "status": "Sent" if match.get("alert_sent") is True else "Not sent",
                    "alert_recipient": (recipient_record or {}).get("name") or "Not recorded",
                    "lead_owner": users.get(match.get("lead_owner"), {}).get("name") or "Unknown agent",
                    "listing_owner": users.get(match.get("listing_owner"), {}).get("name") or "Unknown agent",
                    "area": area, "score": match.get("match_score"),
                    "explanation": match.get("match_reason"),
                    "unsent_reason": None if match.get("alert_sent") is True else _unsent_reason(match, recipient_record, threshold),
                    "created_at": match.get("Created Date"),
                    "lead_url": f"{SITE_URL}/lead/{match.get('lead')}" if match.get("lead") else None,
                    "listing_url": f"{SITE_URL}/listing/{match.get('listing')}" if match.get("listing") else None,
                })
            total_pages = (total_matches + MATCH_PAGE_SIZE - 1) // MATCH_PAGE_SIZE
            return {
                "range": period, "status": status_filter, "threshold": threshold,
                "matches": output, "alerts": output, "page": page,
                "page_size": MATCH_PAGE_SIZE, "total_matches": total_matches,
                "total_pages": total_pages, "has_previous": page > 1 and total_pages > 0,
                "has_next": page < total_pages,
            }
        try:
            key = f"matches:{period}:{status_filter}:page:{page}:threshold:{threshold}"
            return jsonify(cache.get_or_create(key, load))
        except BubbleAPIError as exc:
            return jsonify({"error": str(exc)}), 503

    app.extensions["dashboard_cache"] = cache
    return app


app = create_app()

