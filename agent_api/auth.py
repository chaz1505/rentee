import datetime as dt
import hashlib
import os
import re
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


def issue_login(user_id, client=None, token_type="login"):
    """Issue a hashed, single-use app token using the existing AgentSession type.

    ``match`` tokens are generated per recipient.  The permanent Match id stays
    in the URL and is deliberately not persisted on AgentSession.
    """
    assert_enabled()
    if not user_allowed(user_id): raise AuthError("Agent application access is unavailable", 403)
    if token_type not in {"login", "match"}: raise ValueError("Unsupported login token type")
    now = utcnow()
    if token_type == "login":
        with _rate_lock:
            last = _last_login.get(str(user_id))
            wait = int(os.getenv("AGENT_LOGIN_RATE_LIMIT_SECONDS", "60"))
            if last and (now-last).total_seconds() < wait: raise AuthError("Please wait before requesting another link", 429)
            _last_login[str(user_id)] = now
    token = secrets.token_urlsafe(32)
    expires_at = now + (dt.timedelta(days=int(os.getenv("AGENT_MATCH_TOKEN_TTL_DAYS", "7"))) if token_type == "match" else dt.timedelta(minutes=int(os.getenv("AGENT_LOGIN_TOKEN_TTL_MINUTES", "5"))))
    (client or BubbleClient()).create(TOKEN_OBJECT, {"user": user_id, "token_hash": token_hash(token), "token_type": token_type, "expires_at": iso(expires_at), "revoked": False})
    return token


def issue_match_login(user_id, client=None):
    return issue_login(user_id, client, "match")


def issue_match_link(user_id, match_id, client=None):
    """Build a recipient-specific future Match Alert URL without sending it."""
    if not isinstance(match_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", match_id):
        raise ValueError("Invalid Match id")
    token=issue_match_login(user_id,client)
    base=os.getenv("AGENT_APP_URL","https://app.rentee.asia").rstrip("/")
    return f"{base}/matches/{match_id}#token={token}"


def _valid(record, kind):
    return bool(record and record.get("token_type") == kind and not record.get("revoked") and parse_date(record.get("expires_at")) and parse_date(record["expires_at"]) > utcnow())


def redeem(login_token, client=None):
    assert_enabled(); assert_origin()
    if not isinstance(login_token, str) or not 20 <= len(login_token) <= 256: raise AuthError("This login link is invalid or expired")
    client = client or BubbleClient(); digest = token_hash(login_token)
    with _redeem_lock:
        # Bubble is checked immediately before the best-effort consume.  The
        # process lock prevents same-process races; Bubble has no approved
        # compare-and-set primitive, so cross-process atomicity is not claimed.
        rows, _ = client.search(TOKEN_OBJECT, [{"key":"token_hash","constraint_type":"equals","value":digest}], limit=2)
        if len(rows) != 1: raise AuthError("This login link is invalid or expired")
        record = rows[0]
        kind = record.get("token_type")
        if kind not in {"login", "match"} or not _valid(record, kind) or record.get("used_at"): raise AuthError("This login link is invalid or expired")
        user_id = record.get("user")
        if not user_allowed(user_id) or not client.one("user", user_id): raise AuthError("This login link is invalid or expired")
        consumed_at = iso(utcnow()); client.patch(TOKEN_OBJECT, record["_id"], {"used_at": consumed_at, "expires_at": consumed_at})
        confirmed = client.one(TOKEN_OBJECT, record["_id"])
        confirmed_used = parse_date((confirmed or {}).get("used_at"))
        confirmed_expiry = parse_date((confirmed or {}).get("expires_at"))
        expected_used = parse_date(consumed_at)
        if not confirmed_used or not confirmed_expiry or not expected_used or abs((confirmed_used-expected_used).total_seconds()) > 2 or abs((confirmed_expiry-expected_used).total_seconds()) > 2:
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
