# Agent App performance notes

## Bottlenecks found

- Listings and leads used Bubble cursor pagination, but the frontend explicitly
  requested 100 rows and had no continuation flow.
- The match inbox loaded, resolved, serialized, and returned every eligible
  match in one response. Match detail itself was already record-scoped.
- Home downloaded as many as 5,000 listings, leads, and matches to calculate
  exact network statistics. Bubble's Data API does not expose an aggregate
  count operation that preserves the application's active and deduplication
  rules.
- Relationship IDs were batched within one request, but repeated on later
  requests. Authentication performs the required session and User reads on
  every request; these remain deliberately uncached.

## Changes

- The API page size defaults to 20 via `AGENT_PAGE_SIZE`. Inventory constraints
  and ordering are sent to Bubble before its cursor is applied.
- List and match responses include `page`, `page_size`, `has_more`, and
  `next_page`. The match response also includes its exact deduplicated `total`.
- The frontend loads only the active route's first page, automatically loads
  more near the bottom, deduplicates concurrent calls and records, retains
  prior pages, and offers an inline retry after a continuation failure.
- Match serialization and display-name resolution are limited to the requested
  output page. Eligibility and pair deduplication still run over the complete
  candidate set so page boundaries cannot create missing or duplicate pairs.
- Development, Geo, and agency names use a bounded thread-safe cache controlled
  by `AGENT_REFERENCE_CACHE_TTL_SECONDS` (default 3600). Authenticated,
  user-scoped Home results use `AGENT_DASHBOARD_CACHE_TTL_SECONDS` (default 60).
- Structured logs now include total and Bubble duration, Bubble request count,
  records read/returned, relationship and matching duration, and cache hits and
  misses. They contain no record bodies or credentials.

## Remaining limitations

Exact Match grouping/deduplication requires seeing the full candidate set, so
the backend still scans candidates before slicing the final ordered result.
Likewise, exact network dashboard counts require collection scans on a cache
miss because Bubble Data API has no suitable aggregate endpoint. The 60-second
cache removes this work from repeated dashboard opens but not the first request
to each Python worker. Caches are process-local, are not shared across workers,
and disappear on restart.

Live before/after latency and Bubble request counts require production traffic
and were not fabricated locally. The new timing log supplies those measurements
after deployment. Local verification ran 30 backend tests in 0.067 seconds and
the frontend production build in 0.596 seconds.

Both backend and frontend deployments are required. No deployment was run.
