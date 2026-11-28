# Rentee agent frontend

Standalone mobile-first Phase 1 prototype. It uses local mock data and `localStorage`; it does not call production services or cache data through a service worker.

## Local development

```bash
cd frontend
npm ci
npm run dev
```

Open the URL Vite prints. Build and preview production output with:

```bash
npm run build
npm run preview
```

## Render static-site deployment

- Root directory: `frontend`
- Build command: `npm ci && npm run build`
- Publish directory: `dist`
- Add a rewrite rule (not a redirect): `/*` → `/index.html`

The rewrite is required for direct access to React Router detail URLs. No existing Render service configuration needs to change in this phase.

## Prototype architecture

- `src/data.ts`: relational mock agents, listings, leads, matches, and illustrative network aggregates.
- `src/store.tsx`: local persistence and mutations for review, contact, dismissal, confirmation, deactivation, and reset.
- `src/components.tsx`: reusable navigation, cards, filters, progress, messages, dialogs, and empty states.
- `src/App.tsx`: seven screens and route-level presentation.

Match records are qualified matches. A listing–lead pair has one match ID. Availability and agent-specific review status are separate. The 30-day freshness threshold is prototype-only. Use **Reset demo data** at the bottom of Home to clear saved state.

For a future API integration, replace the seed/load/save operations behind `StoreProvider` with repository calls while retaining its public actions. Authentication and owner authorization must then be enforced server-side.
