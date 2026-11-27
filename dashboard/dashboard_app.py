"""Standalone read-only Rentee operations dashboard."""

from __future__ import annotations

import datetime as dt
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from bubble_api import BubbleAPI, BubbleAPIError, TTLCache, parallel_counts

KL = ZoneInfo("Asia/Kuala_Lumpur")
VALID_RANGES = {"today", "7d", "30d", "all"}
SITE_URL = "https://www.rentee.asia"


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("DASHBOARD_SECRET_KEY"),
        DASHBOARD_PASSWORD=os.getenv("DASHBOARD_PASSWORD"),
        BUBBLE_API_BASE_URL=os.getenv("BUBBLE_API_BASE_URL"),
        BUBBLE_API_TOKEN=os.getenv("BUBBLE_API_TOKEN"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=12),
    )
    if config:
        app.config.update(config)

    cache = TTLCache(60)
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
        return [{"key": "Created Date", "constraint_type": "greater than or equal", "value": start.isoformat()}]

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
    @require_api_auth
    def alerts():
        period = request.args.get("range", "today")
        if period not in VALID_RANGES:
            return jsonify({"error": "Invalid date range"}), 400

        def load():
            client = api()
            matches = client.records(
                "match", alert_date_constraints(period), limit=10,
                sort_field="alert_sent_at", descending=True,
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
                recipient_id = match.get("alert_recipient")
                other_id = match.get("listing_owner") if recipient_id == match.get("lead_owner") else match.get("lead_owner")
                listing = listings.get(match.get("listing"), {})
                lead = leads.get(match.get("lead"), {})
                development = developments.get(listing.get("development"), {})
                area = development.get("name") or development.get("Name")
                if not area:
                    geo = lead.get("Geo") or listing.get("Geo")
                    area = geo if isinstance(geo, str) and not geo.startswith("1") else "Not specified"
                output.append({
                    "id": match.get("_id"), "status": "Sent",
                    "recipient": users.get(recipient_id, {}).get("name") or "Unknown agent",
                    "other_agent": users.get(other_id, {}).get("name") or "Unknown agent",
                    "area": area, "score": match.get("match_score"),
                    "reason": match.get("match_reason"), "sent_at": match.get("alert_sent_at"),
                    "lead_url": f"{SITE_URL}/lead/{match.get('lead')}" if match.get("lead") else None,
                    "listing_url": f"{SITE_URL}/listing/{match.get('listing')}" if match.get("listing") else None,
                })
            return {"range": period, "alerts": output}
        try:
            return jsonify(cache.get_or_create(f"alerts:{period}", load))
        except BubbleAPIError as exc:
            return jsonify({"error": str(exc)}), 503

    app.extensions["dashboard_cache"] = cache
    return app


app = create_app()

