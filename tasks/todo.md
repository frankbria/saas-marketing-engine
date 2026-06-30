# SaaS Marketing Engine — Working Plan

## S3.2 — QA pass/fail tracking + go-live block (#18)  ← ACTIVE
Self-authored plan (no plan comment on issue). No architectural fork — `qa_checklist_item` already
carries `status`/`comment`/`blocking` (S3.1 pre-added them) and the `qa` gate exists (S2.8).
Refs USER_STORIES S3.2, PRD FR-17/FR-18.

Acceptance criteria (issue #18):
- [x] Mark each item pass/fail with optional comments
- [x] Go-live blocked until all blocking items pass
- [x] State → `live` only on full pass

Design (autonomous, no fork):
- **Backend** (`api/private/qa.py`):
  1. `PATCH /qa/{pid}/checklist/{item_id}` body `{status, comment?}` — sets pass/fail + optional
     tester comment, bumps `updated_at`. Gated to `qa` (409 otherwise; meaningless post-`live`).
     404 if item not for product. Returns the item. Mirrors the S2.6 `ChecklistUpdate` PATCH.
  2. `POST /qa/{pid}/go-live` — requires `qa`; requires ≥1 checklist item (can't go live with zero
     QA → 409 "generate the QA checklist first"); blocks (409, lists offending ords) unless **every
     blocking item is `pass`** (non-blocking fails never block); on full pass crosses `qa → live`.
     Race-recheck after read like the other qa routes.
- **Frontend**: `lib/api.ts` adds `QaItemStatus`/`QaChecklistItem` types + `getQaChecklist`,
  `setQaItemStatus`, `goLive`; new `qa-checklist.tsx` client component (per-item pass/fail buttons +
  comment box, "Go live" enabled only when all blocking pass); `page.tsx` fetches items + renders.
- **Tests**: `test_qa_gate.py` (mark pass/fail+comment; blocked on pending/failing blocking item;
  non-blocking fail doesn't block; full pass→`live`; wrong-state 409; empty-checklist 409); extend
  `lib/api.test.ts`.

Verify: `uv run pytest` 100%; ruff+black; dashboard lint+tsc+test+build; demo all 3 ACs with evidence.

---

## S2.8 — Launch checklist emission (#16)
Self-authored plan (issue had ACs only, no plan comment). No architectural fork.
**Branch:** `feat/s2.8-launch-checklist` · Refs USER_STORIES S2.8, TECH_SPEC §6/§6.7, PRD FR-15.

Acceptance criteria (issue #16):
- [ ] Launch checklist generated from **real setup state**
- [ ] State transition `setup_done → qa`

**Key decision (spec-driven, not a fork):** TECH_SPEC line 112 defines the gate as
`setup_done ──(smoke-test pass + checklist emitted)──▶ qa`. S2.7 currently crosses to `qa` on smoke
pass *alone* — before any checklist exists, crossing the gate prematurely. Fix: **S2.8 owns the gate
crossing.** The smoke test (S2.7) becomes record-only; emitting the launch checklist (which requires a
*passed* smoke test) advances `setup_done → qa`. Dictated by the spec → decided autonomously.

The launch checklist is **deterministic** (no LLM/network) — mirrors S2.7's synchronous pattern. It is
*emitted for the human QA gate to verify*; incomplete human-setup items do **not** block the transition
(the smoke pass is the hard gate). Stored folded on the product as `launch_checklist_json` (mirrors
`smoke_test_json`; no new table — `qa_checklist_item` stays reserved for S3.1/S3.2's pass/fail gate).

Steps (TDD RED → GREEN):
1. Add nullable `launch_checklist_json` to `Product` (mirror `smoke_test_json`).
2. `app/modules/qa/launch_checklist.py`: `LaunchChecklistItem(ord,label,detail,ready)`,
   `LaunchChecklist(emitted_at,items)`, `emit_launch_checklist(product, session)` — derives items from
   real setup output (site built / funnel wired / Stripe test price / smoke passed via `smoke_test_json`;
   channels prepared via Channel rows; human setup done/pending via SetupChecklistItem rows).
3. `POST /api/private/qa/{product_id}/launch-checklist`: 404 unknown; 409 not `setup_done`;
   409 smoke absent/not-passed; emit + store; race-guard re-check; advance `setup_done → qa`; return checklist.
4. Strip `result.passed → QA` from `run_smoke` (record-only); update S2.7 docstrings (`qa.py`, `smoke_test.py`).
5. Tests: `tests/test_launch_checklist.py` (emit+transition+store; 409 smoke-not-passed/not-run; 409 wrong
   state; 404; reflects pending human setup; re-run after qa → 409). Update `tests/test_smoke_test.py`
   (smoke pass now keeps `setup_done`).

Verify: `uv run pytest` 100%; `uv run ruff check . && uv run black --check .`; demo setup_done→qa with evidence.

---

## S2.7 — Pre-QA funnel smoke test (#15)
Self-authored plan (issue had ACs only, no plan comment). No architectural fork.

**Goal:** auto-verify a generated site before the human QA gate — broken plumbing never reaches a human.

Acceptance criteria (issue #15):
- [ ] Asserts: site builds + four funnel events fire + Checkout hits correct test price
- [ ] Failure keeps product in `setup_done` (never reaches `qa`)
- [ ] Result surfaced in dashboard

Design (autonomous, no fork):
- **Synchronous** private endpoint `POST /api/private/qa/{product_id}/smoke-test` — the test is fast
  (no LLM, no real network) so it runs inline and returns a verdict the dashboard shows immediately
  (vs. async job-queue: needless indirection for a fast on-demand verify).
- **No prod pollution:** synthetic funnel traffic (visit/lead/checkout/paid) runs against an isolated
  in-memory SQLite DB seeded with a clone of the product. Real `FunnelEvent`/`MetricEvent` tables are
  never written. Funnel paths exercised by calling the real route fns directly (`record_visit`,
  `record_lead`, `start_checkout`, `_attribute_paid_metric`) — no HTTP/portal.
- **build/impression** stages assert the *real* built artifact (`workspace/<slug>/site/index.html`);
  re-building would cost LLM tokens, so the smoke test verifies the build's artifact, not a rebuild.
- Result persisted as one `smoke_test_json` column on `Product` (lazy: no new table; visible via the
  existing product GET).
- Gate: product must be in `setup_done`. Pass → `qa`. Fail → stays `setup_done`.

Stage → AC map (6 stages, all must pass): `build` (artifact exists, non-empty) · `impression` (built
HTML wires the funnel contract: visit-on-load + /visit + /lead + /checkout) · `visit`
(FunnelEvent VISIT) · `signup` (FunnelEvent LEAD) · `checkout` (start_checkout uses
`stripe_price_id` = correct test price) · `paid` (webhook → MetricEvent PAID at `price_amount_cents`).

Steps (TDD) — all done. Backend 215 passed/2 skipped; dashboard 16 tests + lint + tsc + build clean.
1. [x] `models/product.py`: add `smoke_test_json: str | None`.
2. [x] `modules/qa/smoke_test.py`: `StageResult`/`SmokeTestResult` + `run_smoke_test(product)`
   (isolated in-memory exercise of the funnel; real artifact for build/impression).
3. [x] `api/private/qa.py`: `POST /{product_id}/smoke-test` — gate setup_done, run, persist, transition.
4. [x] mount `qa` router in `api/private/__init__.py`.
5. [x] `tests/test_smoke_test.py`: pass→qa + no real-table pollution; each stage failure→stays
   setup_done; 409 wrong-state; 404 unknown.
6. [x] dashboard: `lib/api.ts` (Product.smoke_test_json + SmokeTestResult + runSmokeTest);
   `smoke-test.tsx` panel (run + per-stage badges, gated on setup_done); mount in page.tsx; api.test.ts.

Deviations / assumptions:
- `impression` stage verifies the wired entry hook, not a recorded impression metric — no impression
  plumbing exists in v1 (channel reach is S4.x). Documented in code.
- Gate strictly on `setup_done` per AC. Nothing yet transitions a product *into* `setup_done`
  (human-setup-checklist territory, S2.6/S2.8) — out of scope here.
- New column via `create_all` (no Alembic in v1; consistent with prior stories adding Product fields).

## S2.6 — Channel accounts + human setup checklist + OAuth connect (#14)
Self-authored plan (issue had only ACs, no plan comment). No architectural fork — token-ingestion
connect endpoint is the safe default (full per-provider authorize/callback deferred; untestable
without real provider apps, and S4.8 owns refresh). Mirrors the S2.x setup-handler pattern. TDD.

Acceptance criteria (issue #14):
- [ ] Per enabled channel: generated handles/bios/profile copy from `brand_json`
- [ ] Ordered human checklist: CAPTCHA acct, OAuth consent, ToS, DNS, SPF/DKIM/DMARC, Stripe/banking
- [ ] Warm-up note before any links are posted
- [ ] OAuth connect flows store tokens in the vault; `channel.connect_state` tracked
- [ ] Checklist completion tracked in dashboard

Design (autonomous, no fork):
- **`channel` table** (TECH_SPEC §4): type(blog|reddit|x|instagram|youtube), enabled, autonomous
  (v1 blog/reddit true), account_ref, connect_state(pending|connected|failed), daily_cap, paused.
  `profile_json` folds {handle, bio, profile_copy, warmup_note} (v1 folded-JSON pattern). Unique
  (product_id, type) → idempotent re-runs.
- **`setup_checklist_item` table** — distinct from `qa_checklist_item` (§3 QA pass/fail): setup is
  human done/pending. Fields: product_id, channel_id?, ord, instruction, category, status, updated_at.
- **AI**: one Opus call `generate_channel_profiles` → handles/bios/profile copy only. Warm-up note is
  deterministic (templated per new account). Budget-reserve + inject pattern (mirrors brand/site).
- **Checklist emission**: fully deterministic (no tokens) — global items (account/CAPTCHA, DNS,
  SPF/DKIM/DMARC, Stripe/banking) + per-channel (account creation, OAuth consent, ToS). Idempotent.
- **OAuth connect**: `POST /channels/{pid}/{cid}/connect` writes token via `vault.put_credential`
  (channel-scoped), sets `connect_state=connected`. Known limitation: per-provider authorize/callback
  redirect deferred — dashboard runs each platform's own OAuth and posts the token here.

Steps (TDD) — all done. PR #51. Backend 209 passed/2 skipped; dashboard 15 passed; demo green on all 5 ACs.
1. [x] `models/channel.py` + `models/setup_checklist_item.py`; register in `models/__init__.py`.
2. [x] `ai/client.py`: ChannelProfile/ChannelProfiles + `generate_channel_profiles` + consts.
3. [x] `modules/setup/channels.py`: `setup_channels` handler (upsert channels from brief plan, fold
   profile_json+warmup, emit deterministic checklist, budget gate, no inner commit). Import in main.py.
4. [x] `api/private/channels.py`: POST /{pid}/setup · GET /{pid} · GET /{pid}/checklist ·
   POST /{pid}/{cid}/connect · PATCH /{pid}/checklist/{item_id}. Register in private `__init__`.
5. [x] dashboard: `lib/api.ts` types+fns; `app/products/[id]/channel-setup.tsx`; mount in page.tsx.
6. [x] tests: test_channel_model, test_channels_setup, test_channels_api; extend lib/api.test.ts.
7. [x] ruff + black.

Codex cross-family review: 1 P2 (silent blank profiles when the model omits a requested channel)
→ now raises `RuntimeError` (job fails/retries) + regression test, matching the brand-kit pattern.

## S2.5 — Attribution chain (UTM → lead → Stripe → webhook) (#13)
Self-authored plan (issue had only ACs, no plan comment). No architectural fork.

**Finding:** S2.1–S2.4 already built every link *up to* the webhook: the site template sets the
`first_touch_token` cookie from UTM + fires visit/lead, `funnel_event` stores `first_touch_token`,
and `start_checkout` (S2.3) passes `client_reference_id` + `metadata{first_touch_token, product_id}`.
The webhook (S2.2) verifies the signature but only returns `{received:true}`. The **only** gap is the
webhook *join* + the `metric_event(stage=paid)` write.

**Decisions (autonomous, no fork):**
- New `metric_event` table per TECH_SPEC §4 (`product_id, channel_id?, content_item_id?, stage, value,
  occurred_at, source`). `channel_id`/`content_item_id` stay **null** — those tables don't exist until
  S4.x; the honest attribution available now is token → lead → product. `ponytail:` comment marks it.
- Webhook join is the **primary** attribution (`client_reference_id` → LEAD `FunnelEvent` → `product_id`),
  with checkout `metadata.product_id` as fallback. Unattributable session → ack Stripe (200), write nothing.
- Idempotent on `source = "stripe:<session_id>"` (Stripe redelivers events) — provenance + dedup in one field.
- `value` = `amount_total` (cents). Only `checkout.session.completed` handled; other event types ignored.
- No `stripe` SDK — parse the already-signature-verified JSON body (stdlib), matching repo convention.

Acceptance criteria (issue #13):
- [ ] UTM per published link → first-touch cookie — ✅ already S2.1 (regression-covered by test_site_template)
- [ ] Token persisted onto `lead.first_touch_token` — ✅ already S2.2
- [ ] Passed as Stripe Checkout `client_reference_id` — ✅ already S2.3 (start_checkout)
- [ ] Webhook joins back → `metric_event(stage=paid, channel_id, content_item_id)` — **this story**
- [ ] Integration test: simulated UTM visit → lead → test sub → attributed paid metric — **this story**

Steps (TDD):
1. [ ] `app/models/metric_event.py`: `MetricEvent` + `MetricStage` enum; register in `models/__init__`.
2. [ ] `app/api/public/stripe.py`: parse verified body; on `checkout.session.completed` join token→lead→product
   (fallback metadata) → write paid `metric_event`, idempotent on source. Add `get_session` dependency.
3. [ ] `tests/test_attribution.py`: full-chain integration (visit→lead→signed webhook→paid metric) +
   idempotent redelivery + unattributable ignored + non-paid event ignored.
4. [ ] ruff + black.

## S2.4 — Email capture + welcome email (#12, branch feat/s2.4-welcome-email)
Self-authored plan (issue had only ACs, no plan comment). No architectural fork.

**Finding:** AC#1 (lead row on capture) is **already satisfied** by S2.2's `POST /funnel/{slug}/lead`
→ `_record`. The only new work is the welcome email + the explicit "no drip" boundary.

**Decisions (autonomous, no fork):**
- **SMTP via stdlib `smtplib`/`EmailMessage`** — no new dependency, mirrors the repo's no-SDK
  convention (stripe_api/webhook are stdlib `urllib`/`hmac`). `SME_SMTP_*` config; `smtp_host` unset
  ⇒ email disabled (skip + log), matching the existing "None until configured" pattern.
- **Best-effort send on a FastAPI `BackgroundTasks`** — a slow/down SMTP server must never block or
  500 lead capture (the lead row is the asset). Send errors are caught + logged. `ponytail:` no
  retry/queue/bounce-handling in v1.
- **Sender seam** `get_welcome_sender` (mirrors `get_checkout_creator`) — overridable in tests, no
  network/mocking lib.
- **No drip engine** — exactly one send per captured lead; no scheduler/sequence.

Acceptance criteria (issue #12) — all demoed with outcome evidence:
- [x] `lead` row written on capture — already S2.2; regression assertion added (demo: 1 LEAD row, normalized email + utm)
- [x] One welcome email sends (SMTP/free ESP) — demo: scheduled+sent on capture
- [x] No drip engine in v1 — one send per lead, no scheduler/sequence module

Steps (TDD) — all done. 6 new tests; 176→ full suite green; ruff+black clean.
1. [x] `config.py`: `smtp_host/port(587)/user/password(SecretStr)/from/starttls`.
2. [x] `app/integrations/email.py`: `send_welcome(to, product)` — stdlib SMTP, verified-context
   STARTTLS+login, plaintext body; no-op+log when unconfigured; catch+log all send/build failures.
3. [x] `app/api/public/funnel.py`: `get_welcome_sender` seam + `BackgroundTasks`; `record_lead`
   schedules `send(email, product)` after the row commits (refresh+expunge so the task can read it).
4. [x] `tests/test_welcome_email.py`: capture schedules welcome · visit does not · unconfigured
   no-op · builds+sends (fake SMTP: To/Subject/From + verified TLS context + login) · failure
   swallowed · bad-header (CR/LF) swallowed.
5. [x] ruff + black.

Codex cross-family review (both fixed): P1 STARTTLS without verified context (credentials over
spoofable TLS) → `ssl.create_default_context()`; P2 header construction outside best-effort guard
(CR/LF in product name raises before `try`) → moved message build inside the guarded block + test.

## S2.3 — Stripe configuration (cc_sub, test mode) (#11, branch feat/s2.3-stripe-config)
Self-authored plan (no plan comment on issue; only ACs). No architectural fork.

**Decisions (autonomous):**
- No `stripe` SDK / no new runtime dep — stdlib `urllib` form-encoded REST POSTs, matching the webhook's stdlib precedent (`app/api/public/stripe.py`). `ponytail:` no idempotency-key/retry; add when hardening for live.
- Testability via injection (mirrors `pricing.py`/`site.py`): setup handler takes injected `create=`; checkout endpoint takes session-creator via FastAPI dependency overridable in tests (no mocking lib). Real-API tests gated on `SME_STRIPE_API_KEY`.
- success/cancel URLs from `product.marketing_domain` (fallback `public_api_base_url`).
- Checkout at `POST /api/funnel/{slug}/checkout` — the path the S2.1 template already calls.
- `metric_event(stage=paid)` emission is **S2.5**; S2.3 only carries `client_reference_id`/metadata so S2.5 can join.

Acceptance criteria (issue #11):
- [ ] Create Stripe product + price; store `stripe_price_id` on product
- [ ] Checkout completes a test-mode subscription end-to-end
- [ ] Passes `client_reference_id`/metadata for attribution
- [ ] Webhook → metric_event(stage=paid) + lifecycle (handler in S2.5)

Steps:
1. Config `stripe_api_key` + `app/integrations/stripe_api.py` (create_product/price/checkout_session) + `.env.example`.
2. `app/modules/setup/stripe_setup.py` handler (cc_sub only, idempotent, persists `stripe_price_id`) + private `POST /api/private/setup/{id}/stripe`.
3. Public `POST /api/funnel/{slug}/checkout` → returns `{url}`, passes client_reference_id + metadata.
4. Tests: `test_stripe_setup.py`, `test_checkout.py` (offline-injected + real-API-gated).
5. ruff + black.

## S2.1 — Templated landing site + funnel contract + UTM (#9, branch feat/s2.1-templated-landing-site)
Self-authored plan (no plan comment on issue). No architectural fork — **clear safe default**:
`site-template/` = one self-contained Jinja2 HTML file rendered by the Python engine (§6.1 "the
engine builds each site") → static `index.html` → deployed to an nginx web root keyed by
`marketing_domain`. NOT a second Next.js app (a landing page is static; the four contract pieces
are ~40 lines of vanilla JS). Jinja2 (new dep) justified: autoescaping AI/user copy into a public
page is a trust-boundary XSS guard. Mirrors the S1.2 brand handler pattern. TDD.

Acceptance criteria (issue #9):
- [ ] `site-template/` with contract components: `<EmailCapture>`, `<StripeCheckout>`, `<AnalyticsSnippet>`, UTM capture (first-touch cookie)
- [ ] AI fills copy slots + brand tokens (palette/font/voice from `brand_json`); layout/plumbing constant
- [ ] Static export + deploy to nginx under `product.marketing_domain`
- [ ] All four funnel events fire (verified by S2.7)

Steps (TDD: test first):
1. [ ] `site-template/index.html.j2`: constant layout + plumbing — UTM→first-touch cookie; `visit` beacon on load; `<EmailCapture>` form→`lead`; `<StripeCheckout>` button→`/checkout` carrying `client_reference_id=token`; copy slots + brand CSS-var tokens. Autoescaped.
2. [ ] `ai/client.py`: `SiteContent` (copy slots + concrete design tokens: primary/accent color, font) + `SITE_MODEL`/`SITE_MAX_TOKENS` + `generate_site_content(...)` (opus-4-8, already priced; same parsed_output scan as brand).
3. [ ] `config.py`: `public_api_base_url`, `nginx_sites_root`.
4. [ ] `modules/setup/site.py` (mirrors `brand.py`): `render_site` (pure, Jinja2) · `build_site`→workspace `{slug}/site/` (static export) · `deploy_site`→`{nginx_sites_root}/{domain}/`+vhost · `_real_generate` (budget reserve) · `build_product_site` + `@handler("setup_site")`.
5. [ ] `api/private/setup.py`: `POST /setup/{id}/site` → 202; 404 missing, 409 not `setup_ready`, 400 no brand_json. Wire into private `__init__`; import handler in `main.py`.
6. [ ] `pyproject.toml`: `jinja2`.
7. [ ] `tests/test_site_template.py` (mirrors `test_brand_kit.py`): render contract assertions (4 components + tokens + autoescape) · build/deploy filesystem · budget gate · worker path · route 202/404/409/400 · key-gated real-API integration.

Ponytail boundaries (marked in code):
- Deploy = local FS place + vhost emit; `nginx -s reload`/scp/TLS are operational (S2.7/S6.4 exercise live).
- `/checkout` endpoint + real Stripe session = S2.3; S2.1 wires the call carrying the token. Demoable events: `visit`+`signup`.

## S2.2 — Public funnel-ingest API (split from private) (#10, branch feature/issue-10-public-funnel-ingest)
Self-authored plan (no plan comment; only a CodeRabbit placeholder). No architectural fork.

**Context:** a public landing site cannot POST to the firewalled private dashboard API. Split a
narrow internet-facing public router off, with the security posture an internet endpoint needs:
rate limiting, strict validation, per-product CORS. The private API stays firewalled and untouched.

Acceptance criteria (issue #10):
- [ ] Public router: `POST /api/funnel/{slug}/visit`, `POST /api/funnel/{slug}/lead`, `POST /api/stripe/webhook`
- [ ] Rate-limited + strictly validated; CORS for the product origin only
- [ ] Private dashboard/operator API stays on the firewalled interface (unchanged)
- [ ] nginx fronts the public router internet-facing; private stays allowlisted (deploy docs)

Design decisions (autonomous — no fork):
1. **Routing** — mount public surface router at `/api` (was `/api/public`); move public health to
   `/public/health` so `/api/public/health` is preserved (test_app_boots green). Add `funnel`
   (prefix `/funnel`) + `stripe` (prefix `/stripe`) routers → exact AC paths.
2. **Persistence** — one minimal `FunnelEvent` table covers visit + lead (event_type, product_id,
   email?, utm_*, first_touch_token, created_at). S2.4 (email) / S2.5 (attribution) extend it.
3. **Rate limiting** — tiny in-process fixed-window limiter dependency. **No new dep** (single-process
   VPS v1). Ceiling noted in code; swap for slowapi+Redis if multi-worker.
4. **Per-product CORS** — middleware on `/api/funnel/*` only: echoes `Origin` iff it matches the
   product's `marketing_domain`; answers preflight `OPTIONS`. Stripe webhook is server-to-server.
5. **Stripe webhook** — stdlib HMAC-SHA256 verify (Stripe `t=…,v1=…` scheme) + timestamp tolerance
   (replay guard). **No `stripe` SDK dep** for S2.2 (receive+verify only). Global
   `SME_STRIPE_WEBHOOK_SECRET`; missing secret → reject loudly. Event processing is S2.5.
6. **Validation** — Pydantic: lead `email` format, UTM optional bounded strings, `first_touch_token`
   optional. Unknown slug → 404.

Steps (TDD: test first):
1. [ ] `config.py`: `rate_limit_requests`, `rate_limit_window_seconds`, `stripe_webhook_secret`.
2. [ ] `models/funnel_event.py`: `FunnelEvent` + `FunnelEventType`; register in `models/__init__`.
3. [ ] `api/public/ratelimit.py`: fixed-window limiter dependency (key = slug + client IP).
4. [ ] `api/public/funnel.py`: visit + lead (validate, rate-limit, persist 201).
5. [ ] `api/public/stripe.py`: webhook (stdlib HMAC verify + timestamp tolerance → 200).
6. [ ] `api/public/cors.py` + `main.py`: per-product CORS middleware for `/api/funnel/*`.
7. [ ] `api/public/__init__.py`: health → `/public/health`; include funnel + stripe.
8. [ ] `main.py`: mount public router at `/api`.
9. [ ] tests: `test_public_funnel.py`, `test_stripe_webhook.py`, `test_public_cors_ratelimit.py`; keep `test_app_boots` green.
10. [ ] deploy docs: nginx fronts `/api/funnel` + `/api/stripe`; private stays allowlisted.

## S1.4 — Owner review/edit + approve strategy (#8, branch feat/s1.4-approve-strategy)
Self-authored plan (issue had no plan comment). No architectural fork — the strategy artifacts
already exist (`StrategyBrief` row + `product.brand_json` + `product.price_*`); S1.4 adds
read/edit + an **approve** transition. `StrategyBrief.approved/approved_at` columns already exist. TDD.

Acceptance criteria (issue #8):
- [ ] Dashboard view to review + edit brief, brand, price
- [ ] Approve transitions `strategy → setup_ready`
- [ ] Setup is blocked until approved

Design (safe defaults):
- "Setup blocked until approved": `setup_ready` is reachable **only** via approve (product PATCH
  already refuses `lifecycle_state`); approve refuses unless complete (brief + brand + price-for-cc_sub)
  and product is in `strategy`. `brief.approved` is the flag future setup phases (S2.x) gate on.
- Brief `*_json` fields edited as raw JSON (single operator); server validates well-formedness.
  ponytail: structured form only if raw-JSON editing proves error-prone.

Steps (TDD):
1. [ ] `api/private/strategy.py`: `GET /strategy/{pid}` (brief, 404 none); `PATCH /strategy/{pid}`
   (positioning + `*_json`, reject malformed JSON); `POST /strategy/{pid}/approve` (404/409 not-strategy/
   400 incomplete → `approved`+`approved_at`+`setup_ready`). Add `brand_json` to `ProductUpdate` (+JSON validate).
2. [ ] `tests/test_strategy_review.py`: GET, PATCH valid+malformed, brand_json PATCH, approve happy
   (`strategy→setup_ready`, `approved`), approve 409 wrong-state, approve 400 incomplete, approve 400 no-brief.
3. [ ] `dashboard/lib/api.ts`: brand/price on `Product`, `StrategyBrief` type, get/update product+brief, approve.
4. [ ] `dashboard/app/products/[id]/page.tsx` + client `StrategyReview`: edit brief/brand/price, Save +
   Approve (disabled unless `strategy`), state badge; link product rows → detail.
5. [ ] `dashboard/lib/api.test.ts`: URL/shape for new client fns.

## S1.3 — Pricing recommendation (cc_sub) (#7, branch feat/s1.3-pricing-recommendation)
Self-authored plan (no plan comment on issue). No architectural fork — mirrors S1.2 (Brand Kit)
exactly: single Opus structured-output call grounded in the existing `strategy_brief`, folded onto
the already-present `product.price_*` columns, run via the async worker as a `pricing` job. No new
table, no lifecycle change (pricing is part of the `strategy` phase). Owner-editable via PATCH. TDD.

Acceptance criteria (issue #7):
- [ ] Populates `product.price_amount_cents` + `price_interval`
- [ ] Owner-editable
- [ ] trial/freemium remain unwired (enum value only)

Steps (TDD):
1. [ ] `app/ai/client.py`: `PricingRecommendation{price_amount_cents:int(gt0), price_interval:Literal["month","year"]}`; `PRICING_MODEL`/`PRICING_MAX_TOKENS`; `recommend_pricing(...)` → `messages.parse` on Opus (same adaptive-thinking + scan-for-parsed_output pattern as `generate_brand_kit`), returns `(PricingRecommendation, cost_cents)`.
2. [ ] `app/modules/strategy/pricing.py` (mirrors `brand.py`): load product + its strategy_brief (grounding) → require `monetization_model == CC_SUB` (else RuntimeError — trial/freemium unwired) → budget gate (`month_to_date_cost_cents`) + synthesis reserve → fold `product.price_amount_cents`+`price_interval`, no handler commit, no state change. `@handler("pricing")`.
3. [ ] `app/main.py`: import pricing module to register the handler.
4. [ ] `app/api/private/strategy.py`: `POST /strategy/{id}/pricing` → 202; 404 missing product, 400 if no brief, 400 if not cc_sub.
5. [ ] `app/api/private/products.py`: add `price_amount_cents`/`price_interval` to `ProductUpdate` (owner-editable; PATCH applies generically).
6. [ ] `backend/tests/test_pricing.py` (mirrors `test_brand_kit.py`): schema/persist+state-unchanged/no-brief/not-cc_sub/budget(exceeded,zero-unlimited,capped)/reserve/worker/route(202,404,400×2)/owner-edit PATCH + key-gated real-API integration.

Decisions (autonomous, no fork):
- `price_interval` constrained to `month`/`year` at the LLM-output boundary (`Literal`); product column stays `str` (no migration); PATCH stays `str` to match the column.
- No rationale persisted — no column, AC doesn't need it (YAGNI).
- Pricing gated to `cc_sub` at route + handler — that is how "trial/freemium remain unwired" is enforced.

## S1.2 — Brand Kit generation (`product.brand_json`) (#6, branch feat/s1.2-brand-kit)
Self-authored plan (no plan comment on issue). No architectural fork — mirrors S1.1
exactly: single Opus structured-output call, grounded in the existing `strategy_brief`,
persisted to the already-present `product.brand_json` column, run via the async worker.
No new table, no new lifecycle state (brand is part of the `strategy` phase). TDD.

Acceptance criteria (issue #6) — all demoed with real-API outcome evidence:
- [x] Claude call → `product.brand_json` (name, voice descriptors, visual seeds, tone)
- [x] Voice descriptors **structured** ({descriptor, guidance}) for later reuse by S4.3 (critic) and S4.4 (guard)
- [x] Persisted on the product (no separate table — demo confirms only product/strategy_brief/job_run/credential tables)

Steps (TDD) — all done. 75 backend tests pass (incl. key-gated brand integration); ruff+black clean.
1. [x] `app/ai/client.py`: `VoiceDescriptor{descriptor, guidance}` + `BrandKit{name, tone, voice_descriptors, visual_seeds}`; `generate_brand_kit(...)` → `messages.parse` on Opus, returns `(BrandKit, cost_cents)`.
2. [x] `app/modules/strategy/brand.py` (mirrors `brief.py`): load product + its strategy_brief (grounding) → budget gate (reuse `month_to_date_cost_cents`) + synthesis reserve (all prompt inputs counted) → persist `product.brand_json`, no handler commit. `@handler("brand_kit")`.
3. [x] `app/api/private/strategy.py`: `POST /strategy/{id}/brand` → 202; 404 missing product, 400 if no brief yet.
4. [x] `app/main.py`: import brand module to register the handler.
5. [x] `backend/tests/test_brand_kit.py`: schema/persistence/budget/worker/route + key-gated real-API integration.

Codex cross-family review: P2 reservation completeness (now counts name/description/positioning/pillars + prompt overhead — fixed). P2 untracked spend on a paid-but-unparsed response → same accepted behavior as S1.1 `synthesize_brief`; covered by the shared known limitation below (proper fix = cost ledger on the worker).

Skipped (ponytail): no `brand_kit` table (folded per AC & §177); no separate `raw_ai_output` (brand_json *is* the validated kit); no haiku tier (one synthesis call, not a bulk loop).

## S0.4 — Encrypted credentials vault (Fernet) (#4, branch feat/s0.4-credentials-vault)
Self-authored plan (no plan comment). No architectural fork — schema pinned by TECH_SPEC §4,
crypto/redaction by §9. Single global key for v1 (ponytail: per-product keys deferred). TDD.

Acceptance criteria (issue #4) — all demoed with outcome evidence:
- [x] `credential` model; Fernet encrypt/decrypt with key from env `SME_VAULT_KEY` (not in DB)
- [x] Write/read round-trips; only ciphertext at rest (raw SQLite row: plaintext absent)
- [x] Plaintext never logged (lint rule + log redaction → logs show `***`)
- [x] Test asserts secret absent from captured logs

Steps (TDD) — all done. 40 tests pass; ruff+black clean.
1. [x] dep: `cryptography==45.0.5`.
2. [x] config: `vault_key: SecretStr | None` (env `SME_VAULT_KEY`; SecretStr after review).
3. [x] `app/secrets/vault.py`: Fernet encrypt/decrypt/generate_key; put/get_credential (channel-scoped after codex P2); thread-safe longest-first log redaction.
4. [x] `app/models/credential.py`: §4 fields; safe `__repr__`. Registered in models `__init__`.
5. [x] wire `install_redaction()` into main.py lifespan.
6. [x] tests: roundtrip; ciphertext-at-rest; missing-key raises; channel scoping; redaction; static lint.

Review fixes: codex P2 (channel scoping); CodeRabbit (SecretStr key, locked longest-first redaction, broadened lint terms, runtime test key).

## S1.1 — Codebase ingest → Marketing Brief (#5, branch feat/s1.1-strategy-brief)
Self-authored plan (no plan comment on issue). Adapts TECH_SPEC §4/§5 onto the S0.2 worker
loop + S0.3 product registry. No architectural fork — see deviation on SDK choice below. TDD.

Acceptance criteria (issue #5):
- [x] Ingest README, manifests, `docs/`, route/endpoint names, UI copy; summarize per-file then synthesize (no whole-repo dump)
- [x] Claude call → `strategy_brief` (ICP, pain points, positioning, channel plan, content pillars, cadence)
- [x] Token cost recorded to `job_run`; checked against `product.token_budget_cents_month` (pre-check + mid-loop cap + synthesis reservation)
- [x] `raw_ai_output` stored for debugging
- [~] Integration test on a real repo: non-empty ICP + ≥3 content pillars — written, key-gated (skipped without `SME_ANTHROPIC_API_KEY`; run by the operator)

Steps (TDD) — all done. 61 backend tests pass (1 key-gated integration skipped); ruff+black clean.
1. [x] `app/models/strategy_brief.py` + register in models `__init__`.
2. [x] config: `anthropic_api_key: SecretStr | None`.
3. [x] `app/ai/pricing.py`: `cost_cents` (ceil → never under-bill).
4. [x] `app/ai/client.py`: `summarize_file` (haiku) + `synthesize_brief` (opus-4-8, structured output via `messages.parse`; parsed_output on the text block).
5. [x] `app/modules/strategy/ingest.py`: resolve repo (always re-clone fresh — no stale cache), bounded signal files incl. UI/component source; symlink-escape guarded.
6. [x] `app/modules/strategy/brief.py` + `@handler("strategy_brief")`: budget pre-check + mid-loop cap + synthesis reservation → ingest → summaries → synthesize → upsert → STRATEGY. No handler commit (worker commits atomically with job status+cost).
7. [x] `app/api/private/strategy.py`: `POST /strategy/{product_id}/brief` → 202; wired into private `__init__`.
8. [x] tests: pricing, ingest (incl. symlink + UI), budget (pre-check/cap/reservation/threading), persistence+upsert, worker records cost, route 202/404/400, key-gated integration.
9. [x] `uv add anthropic==0.112.0`. (Skipped a new `.env.example` — no existing convention; the config docstring documents `SME_ANTHROPIC_API_KEY`. YAGNI.)

Codex cross-family review fixes (all addressed): budget overshoot (synthesis reservation + mid-loop cap), handler/worker commit atomicity (no early commit), stale clone (always re-clone), symlink traversal during ingest (resolve-and-contain), UI copy omitted from ingest (broadened signal hints).

Known limitation (documented, deferred): the `strategy_brief` job is costly + non-idempotent on the
S0.2 retry-rollback worker — a mid-run failure (e.g. a transient API error, or summary spend that
crosses a tiny remaining budget) doesn't record its partial spend and can re-spend on retry (≤3×,
bounded to the cheap summary phase since the reservation blocks the expensive synthesis). A proper
fix (incremental cost ledger / resumable jobs) belongs to the shared worker, not S1.1.

Deviations / assumptions:
- **Anthropic SDK, not Managed Agents / "Claude Agent SDK".** S1.1 is one deterministic ingest→summarize→synthesize pipeline our code orchestrates, not an open-ended agent loop. Single structured generation → Messages API w/ structured outputs is the simplest correct surface (per claude-api guidance). Safe default, not a fork.
- Model tiers (§9): `claude-opus-4-8` synthesis, `claude-haiku-4-5` per-file bulk.
- `token_budget_cents_month == 0` = unlimited/unset (onboarding default) → does not block.
- No-mock rule: real API used in the key-gated integration test; worker wiring + persistence tested via an injected stub generator (a seam, not a network mock). DB tests use real SQLite.
- S1.1 produces only `strategy_brief`; brand kit (S1.2) + pricing (S1.3) are later stories.

## Phase: Discovery → PRD → Spec (current)
- [x] Read BRAINSTORM.md transcript
- [x] Run structured brainstorm (3 decision rounds)
- [x] Write PRD.md
- [x] Write USER_STORIES.md
- [x] Frank filled inputs (Auto Author = github.com/frankbria/auto-author; no accounts/brand; autoauthor.app domain; dashboard no-auth/firewalled)
- [x] Write TECH_SPEC.md
- [x] Add G7 no-product-hardcoding rule (Auto Author is fixture only)
- [x] git init + push to github.com/frankbria/saas-marketing-engine (private, gh acct=frankbria verified)
- [x] Multi-perspective debate review (simplicity hawk vs functionality advocate)
- [x] Apply Revision 0.2 to PRD + TECH_SPEC (3 user decisions below)
- [x] Reflow PRD + TECH_SPEC + USER_STORIES bodies to inline all v0.2 deltas (no §0 supersession block; clean self-consistent docs, story IDs aligned across all three)
- [ ] Review/approve v0.2 docs
- [x] Break phases into atomic GitHub issues (#1–#34, 7 phase milestones, type labels; story IDs map 1:1 to issue numbers, S0.1=#1 … S6.4=#34)
- [ ] Start P0 (foundation) build — issues #1–#4

## S0.2 — Storage + scheduler + infra (#2, branch feat/s0.2-storage-scheduler-infra) ✅
Self-authored plan (no plan comment on issue). TDD. 11 tests pass; live round-trip demo'd.
- [x] deps: add `sqlmodel`, `apscheduler` to backend/pyproject.toml (no celery/redis/postgres)
- [x] `app/db.py`: SQLModel engine on SQLite, WAL + busy_timeout via PRAGMA on connect; `init_db()` (metadata.create_all — no alembic in v1), `get_session()`
- [x] `app/models/job_run.py`: JobRun table (id, product_id nullable [no FK — product table is S0.3], kind, status, attempts, token_cost_cents, started_at, finished_at, error, created_at)
- [x] `app/worker.py`: job handler registry + `enqueue()` + `run_due_jobs(session)` (sync, deterministic — increments attempts, retries up to MAX_ATTEMPTS, marks done/failed); `noop` handler
- [x] `app/scheduler.py`: APScheduler BackgroundScheduler — heartbeat enqueues noop + worker tick processes queue
- [x] wire into `main.py` lifespan: init_db + start/stop scheduler
- [x] `infra/deploy/check-ports.sh` + `PORTS.md`: port-conflict check (8010/3010) documented vs VPS (both free)
- [x] tests: WAL on, noop round-trips, retry-on-failure, transient-recover, unknown-kind, no celery/redis/postgres in deps

## S0.3 — Product registry model + API + onboarding form (#3, branch feature/issue-3-product-registry)
Self-authored plan (issue had acceptance criteria, no plan comment). No architectural fork:
schema pinned by TECH_SPEC §4, patterns by existing S0.1/S0.2 code. TDD.

Acceptance criteria:
- [x] `product` model per TECH_SPEC §4 (monetization_model default `cc_sub`, marketing_domain, token_budget_cents_month)
- [x] CRUD API (private router) + onboarding form (name, repo location, description, monetization model, domain, token budget)
- [x] New product → isolated workspace dir + empty credentials vault; lifecycle = `draft`
- [x] Product list view in dashboard
- [x] No operator login (firewalled — nothing to build)

Steps (TDD: test first):
1. [x] Config: `workspace_root` setting (`SME_WORKSPACE_ROOT`, default `./workspace`).
2. [x] `app/models/product.py`: `Product` table w/ all TECH_SPEC §4 fields; `MonetizationModel`+`LifecycleState` StrEnums; defaults cc_sub/draft; `slug` unique-indexed. Register in models `__init__`.
3. [x] `app/workspace.py`: `create_workspace(slug)` makes `{root}/{slug}/` + `{slug}/vault/` (empty cred vault); idempotent. `remove_workspace(slug)`.
4. [x] `app/api/private/products.py`: CRUD (POST slugifies+creates row+workspace+lifecycle=draft, GET list, GET {id}, PATCH {id}, DELETE {id}); pydantic create/update/read; wire into private `__init__`.
5. [x] `dashboard/lib/api.ts`: typed fetch wrapper (base from `NEXT_PUBLIC_API_BASE_URL`).
6. [x] `app/products/page.tsx` (list) + `app/products/new/page.tsx` (form); native Tailwind inputs + Button.

Review fixes (codex cross-family pass):
- [x] P1 CORS: dashboard origin calls private API cross-origin → added config-driven CORSMiddleware (`SME_CORS_ORIGINS`, default localhost:3010) + test.
- [x] P2 lifecycle: dropped `lifecycle_state` from PATCH (transitions belong to the state machine, S1.4/S3.2) + guard test.
- Verified live: 2 products created, workspace+vault on disk, G7 second product, delete removes workspace, lifecycle PATCH ignored. 27 backend + 6 frontend tests pass; build clean.

Deviations / assumptions:
- Vault in S0.3 = empty `vault/` dir; Fernet + `credential` table is S0.4.
- Native styled inputs over 5 new shadcn primitives (smaller diff, internal firewalled tool).
- DELETE included ("CRUD"); also removes workspace dir.
- brand_json/pricing fields present-but-nullable (folded per §4, populated by S1/S2).

## GitHub setup
- Milestones: Phase 0–6 (4/4/8/2/9/3/4 issues)
- Labels: backend, frontend, infra, devops, ai, integration, security
- Issue→story map is sequential: #1=S0.1, #5=S1.1, #9=S2.1, #17=S3.1, #19=S4.1, #28=S5.0, #31=S6.1
- Dependencies expressed by story ID in each issue body (GitHub has no native hard deps)

## Revision 0.2 decisions (2026-06-28 design review)
- Infra: SQLite(WAL) + APScheduler + job_run for v1; Celery/Postgres/Redis/Flower → Phase B only
- Cost: AI tokens = real metered spend, per-product budget + hard stop; Phase B media needs GPU (not on dev VPS) → text-only until separate decision
- Channels: owned-first — blog + email autonomous, Reddit warmed/careful, X/IG/YouTube deferred/human-assisted; drop browser fallback in v1
- Must-fix bug: split public funnel-ingest API from private dashboard API
- Must-add (cheap): attribution chain (UTM→cookie→lead→Stripe→webhook), heartbeat+alerts (zero-reach/shadowban), publish idempotency+novelty, adapter delete()/retract, pre-QA site smoke test, SPF/DKIM/DMARC, rate pacing, OAuth refresh handling
- Guardrail: one LLM critic {score,safety_pass,notes} + non-LLM blocklist + claim-traces-to-brief + first-item/random-10% human spot-check; generator≠critic tier
- Simplify: cc_sub only (keep enum), one site template + AI copy (not bespoke), single welcome email, brand_kit/pricing → JSON on product

## Locked decisions (from brainstorm 2026-06-28)
- Single-owner, multi-product (NOT multi-tenant — no auth/account isolation)
- Product #0: Auto Author · B2C/small-business first · B2E deferred
- Pipeline: Strategy → Setup → [human QA gate] → Crank (autonomous)
- Human-in-loop ONLY at: account/payment/domain setup + pre-launch QA
- Crank fully autonomous: generator→critic quality gate, no per-post approval
- Channels: landing site + IG/X/Reddit/YouTube (multi-channel)
- Content types: text/social + SEO blog (Phase A), short-form video + podcast (Phase B)
- Publishing: API-first, browser-automation fallback
- Monetization: configurable per product (CC-upfront sub / trial / freemium)
- Runtime: FastAPI + Next.js (Nova) dashboard
- Infra: Postgres + Celery + Redis + Flower on Hostinger dev VPS
- Landing sites: AI-bespoke per product + standardized funnel contract (Stripe/email/analytics components)
- Done-state: operator onboards a NEW product through full cycle with zero code changes; crank runs unattended ≥2 weeks with metrics flowing

## Open risks / to verify at build time
- VPS port conflicts (FastAPI/Next/Postgres/Redis/Celery/Flower) — check before binding
- CORS (dashboard ↔ API) before first remote deploy
- Platform API access + ToS for autonomous posting (X/Reddit/YouTube/IG) on own accounts
- Bespoke-site QA cost vs standardized funnel contract — keep the contract strict
- 1-month timeline is aggressive for all 4 content types — Phase B may slip; that's fine

---

## Issue #1 — S0.1 Monorepo scaffold (feature/issue-1-monorepo-scaffold)
Self-authored plan (no architectural fork). TDD where meaningful (app boot + router wiring).

Steps:
1. [ ] Backend `uv` project: pyproject (fastapi/uvicorn/pydantic-settings + dev: pytest/httpx/ruff/black); `app/` package skeleton per TECH_SPEC §2 (api/private, api/public, models, modules/{strategy,setup,qa,crank,metrics}, channels, ai, secrets); `app/main.py` create_app mounting both routers + `/health`; `app/config.py` Settings. **RED**: test app boots + both router prefixes mounted + /health 200.
2. [ ] `dashboard/` Next.js Nova (Nova preset via shadcn; fallback create-next-app + nova init); trivial smoke test; lint + tsc pass.
3. [ ] `.pre-commit-config.yaml`: ruff + black (py), eslint + tsc (ts).
4. [ ] `.github/workflows/ci.yml`: backend pytest + frontend lint/tsc/test on PR to main.
5. [ ] `README.md`: project intro + feature-branch → PR → main flow + how to run/test.

Deviations / assumptions:
- `db.py` (needs SQLModel) and `scheduler.py` (APScheduler) are deferred to **S0.2** — they carry behavior, not layout; S0.1 ships the bootable skeleton + wired routers.
- Backend deps limited to what the scaffold uses (no SQLModel/Celery/etc. yet) — YAGNI.
- Nova applied via the named `nova` shadcn preset (the mandated preset **URL** returns registry 400, so the URL-encoded gray/Hugeicons/Nunito-Sans choices were applied **manually** after scaffolding: removed lucide-react → @hugeicons/react, font → Nunito Sans, baseColor → gray, dark sidebar-primary normalized to gray). The plain Next.js+TS+ESLint+Tailwind fallback was **not** needed and is not an acceptable end state.

Acceptance criteria (from issue #1):
- [ ] backend/ (uv) + dashboard/ (Next.js Nova) per TECH_SPEC §2 layout
- [ ] FastAPI app boots with empty api/private and api/public routers wired
- [ ] Pre-commit hooks: ruff + black (py), lint + tsc (ts) — all pass
- [ ] CI runs backend + frontend tests on PR
- [ ] Feature-branch → PR → main flow documented in README

---

# S3.1 — Generate click-through QA checklist (issue #17)

**Goal:** Engine generates a concrete, ordered "open X, click Y, verify Z" checklist a
non-technical tester can run — covering product login/use AND the payment funnel — persisted
as `qa_checklist_item` rows. Mirrors the existing AI-generation handlers (pricing/brand/site):
one Opus structured call, async via `enqueue` + `@handler`, budget-reserved. Generation runs
while the product is in `qa` state (reached after S2.8). S3.2 adds pass/fail + go-live block.

## Steps (TDD)
1. Model `app/models/qa_checklist_item.py` — `QaChecklistItem` (id, product_id idx, ord,
   instruction, blocking bool, status enum pending|pass|fail, comment, updated_at) +
   `QaItemStatus`; export from `models/__init__.py`.
2. AI client `app/ai/client.py` — `QA_MODEL`, `QA_MAX_TOKENS`, `QaStep`
   (instruction, area product|funnel, blocking), `QaChecklist`, `generate_qa_checklist(...)`.
3. Handler `app/modules/qa/checklist.py` — `_real_generate` (budget reserve like pricing),
   `generate_qa_checklist_items(job, session, *, generate)` → require product in `qa` + a
   strategy brief, validate coverage (>=1 product AND >=1 funnel step) else raise (retry),
   replace existing rows idempotently, insert ord=1..n. Register `@handler("qa_checklist")`.
4. Route `app/api/private/qa.py` — `POST /{id}/checklist` (202, gate lifecycle==qa) +
   `GET /{id}/checklist` (list ordered).
5. Wire `main.py` import to register handler.
6. Tests `tests/test_qa_checklist.py` — worker wiring + persistence (stub generate), budget
   gate, coverage validation, not-qa gate, idempotent regen, GET list; skipped real-API test.

## Acceptance (issue #17)
- [ ] Concrete, ordered "open X click Y verify Z" steps
- [ ] Covers product login/use AND payment funnel
- [ ] Persisted as qa_checklist_item rows
