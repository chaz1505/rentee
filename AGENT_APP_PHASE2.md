# Agent app Phase 2 deployment notes

The agent API is registered on the existing Flask service at `/api/agent`. It is disabled by default and Bubble remains the only persistent store. The frontend never receives Bubble credentials or authentication metadata.

## Security gate and worker requirement

Login redemption uses a process-wide lock around search, validation, consumption confirmation, and session creation. It is safe against concurrent threads only when exactly one Python process is serving requests. It is **not distributed atomicity** and does not cover two Render instances or overlapping old/new processes during a deploy.

Keep `AGENT_APP_ENABLED=false` and `AGENT_REDEMPTION_SINGLE_PROCESS=false` until all of these are true:

1. The backend start command is `gunicorn --workers 1 --threads 4 --bind 0.0.0.0:$PORT app:app` (and `WEB_CONCURRENCY=1`).
2. Render is configured for one service instance.
3. Deploys cannot route redemption traffic to overlapping old and new instances. If this cannot be guaranteed, redemption must remain disabled pending an approved Bubble-side atomic workflow or other approved solution.
4. `AgentSession` is exposed to the trusted server-side Data API credential for constrained create/read/update operations, while Bubble privacy remains private to browsers.

The exact Bubble object name used is `AgentSession`; fields are `user`, `token_hash`, `token_type`, `expires_at`, `used_at`, and `revoked`. Inventory fields used include the case-sensitive names in the brief: `owner`, `development`, `Geo`, `propertyType`, `propertyTypes`, `TransactionType`, `beds`, `baths`, `bedroomsMin`, `bathroomsMin`, `priceRent`, `priceSale`, `budgetRent`, `budgetBuy`, `Furnishing`, `furnishingPreference`, `availability_option`, `cancelled`, `status`, `waMessage`, `last_confirmed_active`, and `Created Date`. Match fields used are `lead`, `listing`, `lead_owner`, `listing_owner`, `match_score`, `match_reason`, side-specific review fields, and `Created Date`.

## Environment

Backend recommended pilot values:

```env
AGENT_APP_ENABLED=false
AGENT_APP_URL=https://app.rentee.asia
AGENT_APP_ALLOWED_ORIGINS=https://app.rentee.asia
AGENT_APP_ALLOWED_USERS=<GWEN_BUBBLE_USER_ID>
AGENT_LOGIN_TOKEN_TTL_MINUTES=15
AGENT_SESSION_DAYS=30
AGENT_INBOX_MIN_SCORE=80
AGENT_INBOX_MAX_AGE_DAYS=30
AGENT_INVENTORY_FRESHNESS_DAYS=30
AGENT_LOGIN_RATE_LIMIT_SECONDS=60
AGENT_BUBBLE_TIMEOUT_SECONDS=12
AGENT_COOKIE_SECURE=true
AGENT_COOKIE_SAMESITE=None
AGENT_REDEMPTION_SINGLE_PROCESS=false
WEB_CONCURRENCY=1
```

An empty allowlist denies everyone. Set `AGENT_REDEMPTION_SINGLE_PROCESS=true` only after satisfying the security gate. The frontend build needs `VITE_AGENT_API_BASE_URL=https://api.rentee.asia`. Mock data is available only in a development build with `VITE_USE_MOCK_DATA=true`; production never falls back to it.

Use `app.rentee.asia` for the static frontend and preferably `api.rentee.asia` for the existing Python service. Point each DNS name to its current Render service, add both custom domains in Render, and verify TLS before enabling cookies. This same-site arrangement supports `Secure; HttpOnly; SameSite=None` credentialed requests without wildcard CORS. No DNS or Render setting is changed by this code.

## Data rules and known gaps

Listings are inactive when `availability_option` is explicitly No or status is closed. Leads are inactive when `cancelled` is true or status is Closed Won/Closed Lost. Freshness uses `last_confirmed_active`, falling back to `Created Date`; it never uses Modified Date or `availability_date`.

Matches are discovered using both owner columns, then access is rechecked against current underlying inventory ownership. Scores below the configured threshold and unscored matches are omitted. Duplicate listing/lead pairs choose the record with the newest `Created Date`, then highest immutable Bubble ID as a deterministic tie-break. The authenticated side's review status is returned; the other side is not.

The repository does not establish an authoritative rule mapping `exposure`, `sharedWith`, and `is_Visible_to` to permission for private counterparty contacts or original messages. Therefore the current implementation returns an owner's original message only for their own inventory and withholds counterparty messages/contact details. This is intentionally restrictive and remains an unresolved dependency before full match-detail parity.

The current home endpoint supplies personal active/freshness counts. Network-wide metrics, Malaysia weekly boundaries, efficient relationship batching, and complete historical/actionable match counters are not yet implemented and must not be represented as production-complete. The frontend retains the approved screens but write controls are no-ops outside explicit local mock mode; a follow-up UI pass should hide them completely and add logout placement.

## Pilot and rollback

1. Verify the AgentSession Data API operations in a non-production Bubble environment without real WhatsApp sends.
2. Configure the domains, exact origin, API base URL, one worker, and Gwen's Bubble User ID.
3. Deploy backend and frontend while both feature flags remain false.
4. Verify `/api/agent/me` returns 503 and `/login` still behaves unchanged.
5. Ensure no overlapping backend process can receive redemption traffic; then set the redemption prerequisite and enable the app.
6. From Gwen's WhatsApp, send `/app`, explicitly press “Open my workspace”, compare listings/leads/matches with Bubble, refresh a direct detail route, and log out.
7. Attempt unauthenticated and guessed-ID access and verify 401/404 responses. Verify a non-allowlisted agent receives no link.

Rollback is setting `AGENT_APP_ENABLED=false` (and preferably `AGENT_REDEMPTION_SINGLE_PROCESS=false`) and redeploying/restarting the existing service. This disables `/app`, redemption, and all protected agent endpoints without changing `/login`, imports, matching, alerts, or the Bubble website. Existing session records may remain in Bubble; they cannot authenticate while the feature is disabled.
