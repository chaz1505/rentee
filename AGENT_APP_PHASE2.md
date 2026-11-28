# Agent app Phase 2 deployment notes

The agent API is registered on the existing Flask service at `/api/agent`. It is disabled by default and Bubble remains the only persistent store. The frontend never receives Bubble credentials or authentication metadata.

## Token redemption model

Login redemption uses a process-wide lock around the final Bubble validation, consumption update, update confirmation, and browser-session creation. Redemption immediately writes the same timestamp to `used_at` and `expires_at`, verifies both through Bubble, and fails closed if Bubble cannot confirm the write. This provides single-use behaviour for sequential requests and concurrent requests handled by one Python process.

Bubble's Data API does not provide an approved conditional-update primitive here. Two processes can therefore validate the same token before either update becomes visible. The restricted pilot explicitly accepts this residual cross-process race; the implementation does **not** claim distributed atomic redemption and does not require single-process deployment as an enablement blocker. `AgentSession` must remain available only to the trusted server-side Data API credential for constrained create/read/update operations, while Bubble privacy remains private to browsers.

The exact Bubble object name used is `AgentSession`; fields are `user`, `token_hash`, `token_type`, `expires_at`, `used_at`, and `revoked`. Inventory fields used include the case-sensitive names in the brief: `owner`, `development`, `Geo`, `propertyType`, `propertyTypes`, `TransactionType`, `beds`, `baths`, `bedroomsMin`, `bathroomsMin`, `priceRent`, `priceSale`, `budgetRent`, `budgetBuy`, `Furnishing`, `furnishingPreference`, `availability_option`, `cancelled`, `status`, `waMessage`, `last_confirmed_active`, and `Created Date`. Match fields used are `lead`, `listing`, `lead_owner`, `listing_owner`, `match_score`, `match_reason`, side-specific review fields, and `Created Date`.

## Environment

Backend recommended pilot values:

```env
AGENT_APP_ENABLED=false
AGENT_APP_URL=https://app.rentee.asia
AGENT_APP_ALLOWED_ORIGINS=https://app.rentee.asia
AGENT_APP_ALLOWED_USERS=<GWEN_BUBBLE_USER_ID>
AGENT_LOGIN_TOKEN_TTL_MINUTES=5
AGENT_MATCH_TOKEN_TTL_DAYS=7
AGENT_SESSION_DAYS=30
AGENT_INBOX_MIN_SCORE=80
AGENT_INBOX_MAX_AGE_DAYS=30
AGENT_INVENTORY_FRESHNESS_DAYS=30
AGENT_LOGIN_RATE_LIMIT_SECONDS=60
AGENT_BUBBLE_TIMEOUT_SECONDS=12
AGENT_COOKIE_SECURE=true
AGENT_COOKIE_SAMESITE=None
```

An empty allowlist denies everyone. The frontend build needs `VITE_AGENT_API_BASE_URL=https://api.rentee.asia`. Mock data is available only in a development build with `VITE_USE_MOCK_DATA=true`; production never falls back to it.

Use `app.rentee.asia` for the static frontend and preferably `api.rentee.asia` for the existing Python service. Point each DNS name to its current Render service, add both custom domains in Render, and verify TLS before enabling cookies. This same-site arrangement supports `Secure; HttpOnly; SameSite=None` credentialed requests without wildcard CORS. No DNS or Render setting is changed by this code.

## Data rules and known gaps

Listings are inactive when `availability_option` is explicitly No or status is closed. Leads are inactive when `cancelled` is true or status is Closed Won/Closed Lost. Freshness uses `last_confirmed_active`, falling back to `Created Date`; it never uses Modified Date or `availability_date`.

Matches are discovered using both owner columns, then access is rechecked against current underlying inventory ownership. Scores below the configured threshold and unscored matches are omitted. Duplicate listing/lead pairs choose the record with the newest `Created Date`, then highest immutable Bubble ID as a deterministic tie-break. The authenticated side's review status is returned; the other side is not.

For an authenticated agent who currently owns either side of a valid Match, the API returns the approved listing/lead facts, both original `waMessage` values, and the counterparty agent's name and agency. Match access does not depend on `exposure`, `sharedWith`, or `is_Visible_to`. Contact resolution separately rechecks match participation and reads the counterparty owner's `User.phone`; it never returns a Listing phone field. Explicit serializers continue to omit notes and all other unapproved fields.

The home endpoint supplies personal active/freshness, actionable, weekly, and lifetime Match counts using the same score, deduplication, participation, and current-ownership checks as the inbox. Weekly boundaries use Malaysia time. Network counts are derived from Bubble data, never production mock fallbacks. Match and related owner lookups are cached per request. The pilot exposes contact lookup but performs no review, dismissal, contact-tracking, availability, activation, or deactivation writes.

Standard `/app` links use five-minute `login` tokens and open the dashboard. Match Alert infrastructure issues separate seven-day `match` tokens per recipient; the permanent Match ID remains in `/matches/MATCH_ID` while the temporary token is held in the URL fragment and removed after processing. Both token types create 30-day browser sessions. Production Match Alert templates and sending behaviour remain unchanged in this phase; Meta dynamic URL-button compatibility must be reverified before template integration.

## Pilot and rollback

1. Verify the AgentSession Data API operations in a non-production Bubble environment without real WhatsApp sends.
2. Configure the domains, exact origin, API base URL, one worker, and Gwen's Bubble User ID.
3. Deploy backend and frontend while both feature flags remain false.
4. Verify `/api/agent/me` returns 503 and `/login` still behaves unchanged.
5. Enable the app only for the restricted allowlist; do not enable production authentication automatically.
6. From Gwen's WhatsApp, send `/app`, explicitly press “Open my workspace”, compare listings/leads/matches with Bubble, refresh a direct detail route, and log out.
7. Attempt unauthenticated and guessed-ID access and verify 401/404 responses. Verify a non-allowlisted agent receives no link.

Rollback is setting `AGENT_APP_ENABLED=false` and redeploying/restarting the existing service. This disables `/app`, redemption, and all protected agent endpoints without changing `/login`, imports, matching, alerts, templates, or the Bubble website. Existing session records may remain in Bubble; they cannot authenticate while the feature is disabled.
