# S6.1 — Attributed funnel + revenue dashboard (issue #31)

**Plan source:** self-authored (no plan on issue). **Refs:** USER_STORIES S6.1, PRD FR-29, TECH_SPEC §6.6/§8.
**Branch:** `feat/s6.1-attributed-funnel`

## Current-state findings (Phase 2)

- Funnel data is split across two tables: `metric_event` (impression, paid — cents in `value`) and
  `funnel_event` (visit, lead/signup — the only table carrying UTM + `first_touch_token`).
- The S2.5 webhook join stops at `product_id` (`api/public/stripe.py:98`) — `channel_id`/
  `content_item_id` stay NULL on paid rows. Comment says attribution "fills in during P6" (= now).
- Published bodies carry no UTM-tagged links; `publish.py:240` comment defers attribution to P6.
  Precedent for publish-time body transforms exists (Reddit `_body_with_marker`).
- `app/modules/metrics/` exists and is empty; private router registry comments a `metrics` router
  comes later. No metrics read endpoint exists anywhere.
- Dashboard: Next.js 16 App Router, sections under `app/products/[id]/`, typed client `lib/api.ts`
  (`apiFetch` → `/api/private`), async server components with try/catch→empty-state, Vitest for
  `lib/api.test.ts` only. No chart lib; convention is minimal Tailwind primitives.

## Design decisions (autonomous — no architectural fork)

1. **UTM convention:** published marketing-domain links get
   `utm_source=<channel.type>&utm_medium=<content_type>&utm_campaign=<product.slug>&utm_content=sme-<content_item.id>`.
   `sme-` prefix makes `utm_content` unambiguous to parse back.
2. **Threading at publish time:** in `publish_scheduled`, rewrite marketing-domain URLs in
   `item.body` before `adapter.publish`; persist the threaded body. Bodies without a site link are
   left as-is (a post that never links to the product can't drive attributable visits).
3. **Webhook-time join (spec §6.6 mandates it):** `_attribute_paid_metric` resolves
   `lead.utm_content` → ContentItem (validated against product) → writes `channel_id` +
   `content_item_id` on the paid MetricEvent. Fallback: `lead.utm_source` → channel type →
   `channel_id` only.
4. **Read endpoint:** `GET /api/private/metrics/{product_id}/funnel` → stage totals + per
   channel/content attribution rows. Visits/signups attributed at query time from their own
   UTM fields (funnel_event); impressions/paid from metric_event columns. Rollup logic in
   `app/modules/metrics/funnel.py`.
5. **Dashboard:** server-component section `app/products/[id]/funnel.tsx` (stage tiles + CSS bar +
   attribution table, hand-rolled Tailwind, no chart dep), rendered from `products/[id]/page.tsx`.

## Steps

1. **UTM threading (backend)** — new `app/modules/metrics/utm.py` (build/thread/parse helpers),
   edit `app/modules/crank/publish.py`. Tests first: `tests/test_utm_threading.py`.
2. **Webhook join completion (backend)** — edit `app/api/public/stripe.py`
   `_attribute_paid_metric`. Tests first: extend `tests/test_attribution.py`.
3. **Funnel rollup endpoint (backend)** — new `app/modules/metrics/funnel.py`,
   new `app/api/private/metrics.py`, register in `api/private/__init__.py`.
   Tests first: `tests/test_metrics_api.py` (mimic `test_content_api.py` fixture pattern).
4. **Dashboard funnel section (frontend)** — `lib/api.ts` types + `getFunnel(productId)`,
   `lib/api.test.ts` block, `app/products/[id]/funnel.tsx`, wire into `page.tsx`.
   Depends on step 3's response contract (fixed below, so it can run in parallel).

### Endpoint contract (steps 3↔4)

```json
GET /api/private/metrics/{product_id}/funnel
{
  "stages": {"impressions": 0, "visits": 0, "signups": 0, "paid": 0},
  "revenue_cents": 0,
  "rows": [
    {"channel_id": 1, "channel_type": "reddit", "content_item_id": 7,
     "title": "...", "external_url": "...",
     "impressions": 0, "visits": 0, "signups": 0, "paid": 0, "revenue_cents": 0}
  ]
}
```
Unattributed visits/signups/paid roll into a row with `channel_id: null, content_item_id: null`.
404 for unknown product. Per-product only (portfolio roll-up deferred, TECH_SPEC §14).

## Acceptance criteria

- [x] Per-product funnel: impressions → visits → signups → paid → revenue — demoed live
  (seed → visit → lead → signed webhook → `GET /metrics/1/funnel` returned all stages +
  revenue_cents 4900; dashboard section rendered the same data)
- [x] Each conversion joinable to the channel/content item that drove it — demoed live
  (threaded UTM link → funnel events carried `utm_content=sme-1` → paid row attributed to
  the reddit channel + content item in the rollup)
- [x] Portfolio roll-up deferred until >1 product — verified by absence (per-product
  endpoint only)

## Post-review hardening

- Cross-family (codex) P2: funnel row hydration now re-checks `product_id` ownership on
  channel/content lookups (no FK backs those ids) — fixed + regression test (`bf508db`).
- Internal review + deslop scan: clean, no findings above threshold.
- PR: https://github.com/frankbria/saas-marketing-engine/pull/69

## Test strategy

- AC1: `test_metrics_api.py` seeds both tables, asserts stage totals + revenue sum.
- AC2: `test_attribution.py` extension drives the full chain (threaded UTM → visit/lead with
  `utm_content` → signed webhook → paid row carries channel/content ids); `test_metrics_api.py`
  asserts rows group by content item. `test_utm_threading.py` covers link rewrite + parse.
- AC3: deferral — verified by absence (no cross-product endpoint added).

## Known limitations (→ PR)

- `impressions` = publish events (value 1 per post), not real reach; reach collection is S6.2+.
- Visits only fire from the landing page snippet; blog pages don't carry it yet.
- Conversions whose lead lacks UTM stay product-attributed only (shown as unattributed row).
