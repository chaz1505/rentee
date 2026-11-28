# Agent App performance notes

## Follow-up findings

The earlier pagination, reference cache, dashboard cache, infinite scrolling,
authentication and timing diagnostics remain in place. The follow-up found
that Home still downloaded up to 5,000 rows from each global collection on a
cold personal-dashboard cache, Match relationship prefetch loaded Users before
score rejection, the Bubble client did not pool connections, the actionable
cutoff was hardcoded to 30 days, and inventory routes started a complete Match
load in the browser.

## Changes

- `/home` now reads listings and leads with `owner = current user` constraints.
  It returns personal content without global statistics. The frontend starts
  `/network-stats` independently, so a slow or failed network refresh cannot
  block or fail the personal dashboard.
- `/network-stats` is authenticated but globally cached per Python process.
  `AGENT_NETWORK_CACHE_TTL_SECONDS` defaults to 300. A lock gives cache misses
  single-flight behaviour within a process. The scan has no 5,000-row ceiling.
- Network Match discovery is constrained to the current Malaysian week. Pair
  deduplication is unchanged: if a pair has any Match this week, its newest
  Match is necessarily this week. Network calculations do not resolve condo,
  Geo, agency, User, or WhatsApp fields.
- Match candidates are deduplicated and rejected for missing, invalid, or low
  scores before listing/lead reads. Listings and leads are bulk-read in
  parallel, current ownership and activity are revalidated, and pairs are then
  deduplicated. User and display-name reads occur only for the selected page.
- Independent owner inventory, owner Match, and listing/lead relationship reads
  use at most `AGENT_BUBBLE_MAX_PARALLEL_REQUESTS` workers (default 4). Bubble
  pagination within one query remains sequential.
- Bubble calls now use thread-local `requests.Session` pools. Sequential calls
  in a thread reuse connections, while concurrent workers do not share a
  Session. Existing connect/read timeouts and redacted error handling remain.
- Actionable inbox age consistently uses `AGENT_INBOX_MAX_AGE_DAYS` (default
  30). Historical eligibility is not age-limited.
- Listings and Leads no longer start the complete Match inbox. Existing data
  stays rendered during refresh and pagination. A request generation guard
  prevents responses started for a logged-out or previous account from being
  applied. Match badges are omitted until loaded rather than rendered as zero.
- Timing logs now report candidate Matches, related listings/leads, Users read,
  and Match rows serialized in addition to cumulative Bubble time and actual
  endpoint elapsed time.

## Match pagination: exact behaviour

Match pagination remains response-level, not database-level. Before page 1 the
backend discovers all candidates from owner fields plus current owned inventory
(the latter preserves legacy Matches with missing owner fields), deduplicates
candidate IDs, score-filters, bulk-loads only relationships for qualifying
candidates, validates current ownership/activity, and deduplicates pairs. It
then sorts and slices the requested page. Only that page loads counterparty
Users, resolves display names, and serializes rows.

Every later Match page currently repeats candidate discovery and eligibility
validation. This is required for exact totals, stable pair grouping/order, and
authoritative current-ownership checks without another datastore. Opening a
listing or lead list/detail no longer causes this scan; opening the Match inbox
does. Logs provide the requested measurable counts as `match_candidates`,
`match_listings`, `match_leads`, `match_users`, and `match_serialized`.

No candidate cache is used as an access-control decision, and authentication
and session validation remain uncached.

## Bubble workload and cache behaviour

Before this follow-up, a cold Home performed up to 150 global pagination calls
(50 each for 5,000 listings, leads, and Matches), then personal Match work. It
also silently treated those caps as exact totals. Home now performs zero global
collection calls. Its personal listing/lead reads and two owner Match queries
run in two parallel pairs, followed by legacy inventory Match discovery and
relationship batches.

A cold `/network-stats` still completely scans listings and leads because the
custom active rules and distinct-owner count cannot be represented by a safe
Bubble aggregate. It scans only this week's Match rows, never all historical
Matches. A warm network request performs zero statistic Bubble reads; only the
required authentication/session reads remain. The cache and single-flight lock
are process-local, so separate workers can each refresh once after expiry.

Cold/warm latency was not fabricated without production data. Structured logs
now expose request counts, cumulative Bubble duration, endpoint elapsed time,
and stage record counts for deployment measurement. Bounded parallelism can
reduce elapsed time but does not reduce query count and can create up to four
simultaneous Bubble requests per processing operation.

## Configuration

- `AGENT_PAGE_SIZE=20`
- `AGENT_DASHBOARD_CACHE_TTL_SECONDS=60`
- `AGENT_NETWORK_CACHE_TTL_SECONDS=300` (new)
- `AGENT_REFERENCE_CACHE_TTL_SECONDS=3600`
- `AGENT_BUBBLE_MAX_PARALLEL_REQUESTS=4` (new, capped at 4)
- `AGENT_BUBBLE_PROCESS_MAX_IN_FLIGHT=8` (new, per-process safety bound)
- `AGENT_INBOX_MAX_AGE_DAYS=30` (now used consistently)
- `AGENT_INBOX_MIN_SCORE=80`

## Remaining limitations

Exact Match pages still require the complete user candidate pipeline on every
page. Exact network active and distinct-agent statistics still require complete
listing and lead scans on a process-local cache miss. Cache locks are not shared
between server processes. Bubble rate-limit responses use the existing safe
failure path; there is no automatic retry that could multiply load. The app
remains read-only and no Bubble schema, privacy rule, WhatsApp import, Match
generation, alert, database, or infrastructure was changed.

Local verification: 34 backend regression tests passed and the frontend
production build completed successfully. No deployment was run.
