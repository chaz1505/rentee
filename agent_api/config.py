import os


def flag(name, default=False):
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def allowed_users():
    return {item.strip() for item in os.getenv("AGENT_APP_ALLOWED_USERS", "").split(",") if item.strip()}


def permitted_origins():
    raw = os.getenv("AGENT_APP_ALLOWED_ORIGINS", os.getenv("AGENT_APP_URL", "https://app.rentee.asia"))
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


def enabled():
    return flag("AGENT_APP_ENABLED")


def user_allowed(user_id):
    return bool(user_id) and str(user_id) in allowed_users()
