import datetime as dt
import hashlib
import os
import secrets
import threading

from flask import current_app, request

from .bubble_client import BubbleClient, BubbleUnavailable
from .config import enabled, flag, permitted_origins, user_allowed

COOKIE_NAME = "rentee_agent_session"
TOKEN_OBJECT = "AgentSession"
_redeem_lock = threading.Lock()
_rate_lock = threading.Lock()
_last_login = {}


class AuthError(RuntimeError):
    def __init__(self, message="Authentication required", status=401):
        super().__init__(message); self.status = status


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def parse_date(value):
    if not value: return None
    try: return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError: return None


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def assert_origin():
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if not origin or origin not in permitted_origins():
        raise AuthError("Request origin is not permitted", 403)


def assert_enabled():
    if not enabled(): raise AuthError("Agent application is unavailable", 503)


def redemption_runtime_safe():
    # This explicitly acknowledges the deployment-level limitation. Render must
    # have one active process and non-overlapping deploys for this to be true.
    return flag("AGENT_REDEMPTION_SINGLE_PROCESS") and os.getenv("WEB_CONCURRENCY", "1") == "1"


def issue_login(user_id, client=None):
    assert_enabled()
    if not user_allowed(user_id): raise AuthError("Agent application access is unavailable", 403)
    now = utcnow()
    with _rate_lock:
        last = _last_login.get(str(user_id))
        wait = int(os.getenv("AGENT_LOGIN_RATE_LIMIT_SECONDS", "60"))
        if last and (now-last).total_seconds() < wait: raise AuthError("Please wait before requesting another link", 429)
        _last_login[str(user_id)] = now
    token = secrets.token_urlsafe(32)
    ttl = int(os.getenv("AGENT_LOGIN_TOKEN_TTL_MINUTES", "15"))
    (client or BubbleClient()).create(TOKEN_OBJECT, {"user": user_id, "token_hash": token_hash(token), "token_type": "login", "expires_at": iso(now+dt.timedelta(minutes=ttl)), "revoked": False})
    return token


def _valid(record, kind):
    return bool(record and record.get("token_type") == kind and not record.get("revoked") and parse_date(record.get("expires_at")) and parse_date(record["expires_at"]) > utcnow())


def redeem(login_token, client=None):
    assert_enabled(); assert_origin()
    if not redemption_runtime_safe(): raise AuthError("Secure login redemption is not available", 503)
    if not isinstance(login_token, str) or not 20 <= len(login_token) <= 256: raise AuthError("This login link is invalid or expired")
    client = client or BubbleClient(); digest = token_hash(login_token)
    with _redeem_lock:
        rows, _ = client.search(TOKEN_OBJECT, [{"key":"token_hash","constraint_type":"equals","value":digest},{"key":"token_type","constraint_type":"equals","value":"login"}], limit=2)
        if len(rows) != 1: raise AuthError("This login link is invalid or expired")
        record = rows[0]
        if not _valid(record, "login") or record.get("used_at"): raise AuthError("This login link is invalid or expired")
        user_id = record.get("user")
        if not user_allowed(user_id) or not client.one("user", user_id): raise AuthError("This login link is invalid or expired")
        consumed_at = iso(utcnow()); client.patch(TOKEN_OBJECT, record["_id"], {"used_at": consumed_at})
        confirmed = client.one(TOKEN_OBJECT, record["_id"])
        confirmed_used = parse_date((confirmed or {}).get("used_at"))
        expected_used = parse_date(consumed_at)
        if not confirmed_used or not expected_used or abs((confirmed_used-expected_used).total_seconds()) > 2:
            raise AuthError("Secure login could not be confirmed", 503)
        session_token = secrets.token_urlsafe(32)
        days = int(os.getenv("AGENT_SESSION_DAYS", "30"))
        client.create(TOKEN_OBJECT, {"user":user_id,"token_hash":token_hash(session_token),"token_type":"session","expires_at":iso(utcnow()+dt.timedelta(days=days)),"revoked":False})
        return session_token, days


def authenticate(client=None):
    assert_enabled(); token = request.cookies.get(COOKIE_NAME)
    if not token: raise AuthError()
    client = client or BubbleClient()
    rows, _ = client.search(TOKEN_OBJECT, [{"key":"token_hash","constraint_type":"equals","value":token_hash(token)},{"key":"token_type","constraint_type":"equals","value":"session"}], limit=2)
    if len(rows) != 1 or not _valid(rows[0], "session"): raise AuthError()
    user_id = rows[0].get("user")
    if not user_allowed(user_id): raise AuthError("Access denied", 403)
    user = client.one("user", user_id)
    if not user: raise AuthError()
    return user, rows[0], client


def cookie_options(days):
    return {"httponly":True,"secure":flag("AGENT_COOKIE_SECURE", True),"samesite":os.getenv("AGENT_COOKIE_SAMESITE","None"),"path":"/api/agent","max_age":days*86400}
