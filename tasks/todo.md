# Issue #33 — S6.3 Content calendar + spot-check queue

**Plan source**: self-authored (no plan existed on the issue). Derived from USER_STORIES S6.3 (`USER_STORIES.md:198-201`), PRD FR-30/FR-27/FR-29, TECH_SPEC §content_item/§metric_event, and codebase exploration (backend, frontend, specs).
**Branch:** `feature/issue-33-content-calendar-spot-check`

## Acceptance criteria (from issue)
- [ ] Calendar shows generated / critic-passed / published / retracted + performance
- [ ] Spot-check items surfaced for async review

## Key design decisions (made autonomously — no architectural fork)
1. **New endpoint** `GET /api/private/content/{product_id}/calendar` rather than widening `GET /content/{product_id}` — the existing endpoint's published+retracted contract is consumed by `published-content.tsx`; changing it risks breaking that view. Additive endpoint is safer.
2. **Server-side metrics join**: the calendar endpoint embeds per-item performance (impressions/visits/signups/paid/revenue_cents) by reusing `funnel_rollup` (`app/modules/metrics/funnel.py`), joined on `content_item_id`. Keeps the dashboard "reads state, never heavy work" (TECH_SPEC:345) and gives one testable contract.
3. **All statuses returned**, not just the four in the AC — the story's goal is "trust the crank is running"; hiding critic_failed/guard_failed/scheduled/publish_failed would undermine that. The four AC statuses get first-class badges.
4. **No "mark reviewed" column** on spot-check items. TECH_SPEC's content_item model has no reviewed field; FR-27/S4.9 define review as optional/async and never-blocking. Adding an ack column is out of scope (YAGNI) — recorded as a Known Limitation in the PR.
5. **Calendar UI = month grid, no new dependencies**: plain `Date` + Tailwind grid in a `"use client"` component with prev/next month nav. Date anchor per item: `published_at ?? scheduled_for ?? created_at`. Bucketing logic lives in `dashboard/lib/calendar.ts` (pure, unit-testable).
6. **Placement**: new section `dashboard/app/products/[id]/content-calendar.tsx` rendered from the product detail page — matches every prior S-story section (funnel, spot-check queue, published-content). No new route.
7. **Spot-check surfacing**: existing `spot-check-queue.tsx` section (S4.9) remains the review queue; calendar entries with `spot_check=true` additionally get a visible marker, tying AC(2) into the calendar view.

## Steps

### 1. Backend calendar endpoint (TDD)
- Test file: `backend/tests/test_content_calendar_api.py` (mirror `test_content_api.py` fixtures: tmp SQLite + dependency override + TestClient)
  - Returns items across ALL statuses (seed generated, critic_passed, published, retracted, critic_failed)
  - Each item carries `id, channel_id, content_type, title, status, spot_check, critic_score, scheduled_for, published_at, created_at, external_url`
  - Items with metric events get `metrics {impressions, visits, signups, paid, revenue_cents}`; items without get zeros/null
  - 404 on unknown product; empty list on product with no content
- Impl: `backend/app/api/private/content.py` — `GET /{product_id}/calendar`; reuse `funnel_rollup` for the metrics join. No schema change, no `_ADDITIVE_COLUMNS` entry needed.

### 2. Frontend API client (TDD)
- Test: new `describe` block in `dashboard/lib/api.test.ts` mirroring the S6.1 `getFunnel` block (mock fetch, assert URL/shape)
- Impl: `dashboard/lib/api.ts` — `CalendarItem` type + `getContentCalendar(productId)`

### 3. Calendar bucketing helper (TDD)
- Test: `dashboard/lib/calendar.test.ts` — month-grid generation (leading/trailing blanks, day buckets), date-anchor rule `published_at ?? scheduled_for ?? created_at`, month boundary/timezone-safe (UTC date parts)
- Impl: `dashboard/lib/calendar.ts` — pure functions, no deps

### 4. Calendar UI section
- New: `dashboard/app/products/[id]/content-calendar.tsx` — server wrapper fetches via `getContentCalendar`, degrades to empty on error (page.tsx pattern); client grid component with month nav (pattern: `qa-checklist.tsx` for client interactivity, `funnel.tsx` for tiles/tables)
- Status badges for generated/critic_passed/published/retracted (+ muted badges for other statuses); spot-check marker on flagged items; per-item performance (impressions, revenue) shown compactly; `formatCents` pattern from `funnel.tsx`
- Icons via `@hugeicons/react` only

### 5. Wire into product page
- `dashboard/app/products/[id]/page.tsx`: render `<ContentCalendar />` near `<SpotCheckQueue />` / `<Funnel />`

### 6. Quality gates
- Backend: `uv run pytest tests/test_content_calendar_api.py tests/test_content_api.py tests/test_spot_check.py tests/test_metrics_api.py` (targeted locally; full suite gates in CI)
- Frontend: `npm test`, `npm run typecheck`, `npm run lint`
- Deslop scan, internal review, demo (agent-browser walkthrough of both ACs), CI green, merge

## Step dependencies
- Steps 1 and 3 are independent. Step 2 depends on 1's contract (defined above, so parallelizable). Step 4 depends on 2+3. Step 5 depends on 4.

## Assumptions (self-authored plan)
- Per-item "performance" = the S6.1 attribution rollup (FR-29 chain), not heartbeat data (per-channel/day, wrong grain)
- The S4.9 queue endpoint + section already satisfy the surfacing half of AC(2); S6.3 integrates rather than rebuilds it
- No auth (v1 private interface); no pagination needed at v1 volumes
