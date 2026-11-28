# Rentee dashboard

A standalone, read-only Flask dashboard for Rentee platform activity. It does not import or depend on the main Rentee application.

## Environment variables

- `BUBBLE_API_TOKEN` — Bubble Data API bearer token.
- `BUBBLE_API_BASE_URL` — production API root, normally `https://www.rentee.asia/api/1.1`.
- `DASHBOARD_PASSWORD` — a strong, unique dashboard password.
- `DASHBOARD_SECRET_KEY` — a long random value used to sign sessions (for example, output from `python -c "import secrets; print(secrets.token_hex(32))"`).
- `MATCH_ALERT_MIN_SCORE` — optional inclusive alert threshold used for dashboard inference; defaults to `80`.

The service fails closed when authentication or Bubble credentials are absent. Bubble credentials are used only by the server. The Bubble client exposes GET operations only.

## Render deployment

Create a separate free Render web service connected to this repository:

- Root directory: `dashboard`
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn --workers 1 --threads 4 --bind 0.0.0.0:$PORT dashboard_app:app`
- Health check path: `/health`

Add the four required environment variables in Render, plus `MATCH_ALERT_MIN_SCORE` if production uses a value other than `80`. Point `dashboard.rentee.asia` to the new service using Render's custom-domain instructions. Secure cookies require HTTPS, which Render provides for its hostname and configured custom domains.

## Local development and tests

Secure cookies are intentional. For browser-based local development, use HTTPS or temporarily override `SESSION_COOKIE_SECURE` only in a local runner; never disable it on Render.

```sh
cd dashboard
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The UI refreshes on initial load, date-range changes, and the Refresh button. It does not poll. Successful responses are cached for 60 seconds; errors are never converted to zero values or cached.

## Agent App performance configuration

`AGENT_PAGE_SIZE` defaults to `20`. `AGENT_REFERENCE_CACHE_TTL_SECONDS`
defaults to `3600`, and `AGENT_DASHBOARD_CACHE_TTL_SECONDS` defaults to `60`.
The caches are bounded, thread-safe, process-local memory caches. They are
cleared by service restarts and are not shared between Python workers, so a
worker may independently refresh the same value. Authentication, revocation,
and ownership checks are never cached; dashboard cache lookup happens only
after authentication.
