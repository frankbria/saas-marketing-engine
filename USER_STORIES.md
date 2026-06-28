# User Stories — SaaS Marketing Engine (SME)

*Version 0.2 · 2026-06-28 · Companion to PRD.md + TECH_SPEC.md*
*Changelog v0.2: renumbered to the v1 phases — SQLite/APScheduler, owned-first channels, cc_sub-only, templated site, merged+hardened guardrail, plus public/private API split, attribution, failure detection, idempotency, retract, pre-QA smoke test.*

Personas: **Owner** (Frank), **Operator** (Greg), **QA tester**, **Engine** (autonomous).
Stories are grouped by epic = development phase, and each is sized to become one or a few atomic
GitHub issues. Story IDs match TECH_SPEC §13. Acceptance criteria (AC) are written to be testable.

---

## Epic 0 — Foundation

### S0.1 — Monorepo scaffold
**As the** Owner, **I want** a FastAPI + Next.js (Nova) monorepo with pre-commit hooks and CI,
**so that** every later module lands in a consistent, tested structure.
- AC: `uv` backend + Next.js Nova frontend in one repo; FastAPI exposes **two routers** (private dashboard API, public funnel API); pre-commit (ruff/black/lint/tsc) passes; CI runs tests on PR; feature-branch → PR → main documented.

### S0.2 — Storage + scheduler + infra
**As the** Operator, **I want** SQLite (WAL) + APScheduler + a `job_run` worker loop running locally and on the VPS,
**so that** state and scheduled jobs exist before any module needs them — without standing up a queue cluster.
- AC: SQLModel on SQLite(WAL); APScheduler triggers a no-op `job_run` that round-trips with a retry column; **VPS port conflicts checked + documented before binding** (FastAPI :8010, dashboard :3010); no Celery/Redis/Postgres in v1.

### S0.3 — Product registry + onboarding form
**As the** Operator, **I want** to register a product (name, repo location, description, monetization model, marketing domain, token budget) via a form,
**so that** the engine has a unit to operate on.
- AC: Product persists to SQLite with isolated workspace + empty credentials vault; `monetization_model` defaults to `cc_sub`; lifecycle = `draft`; appears in a product list. (No operator login — firewalled internal tool.)

### S0.4 — Encrypted credentials vault
**As the** Owner, **I want** per-product secrets encrypted at rest and never logged,
**so that** API tokens and Stripe keys are safe on a shared VPS.
- AC: Fernet write/read round-trips; ciphertext at rest; secrets absent from logs (lint rule + log redaction); single global `SME_VAULT_KEY` from env.

---

## Epic 1 — Strategy

### S1.1 — Codebase ingest → Marketing Brief
**As the** Engine, **I want** to analyze a product's codebase + description and produce a Marketing Brief,
**so that** setup and content have a strategy to follow.
- AC: brief includes ICP, pain points, positioning, channel plan, content pillars, cadence; per-file summarize-then-synthesize (no whole-repo dump); token cost recorded to `job_run` and checked against the product budget; generated via Claude Agent SDK.

### S1.2 — Brand Kit generation
**As the** Engine, **I want** to produce a brand kit (name/voice/visual seeds) stored as `product.brand_json`,
**so that** every downstream asset is on-brand.
- AC: brand JSON persisted on the product; includes voice descriptors used later by the critic+safety call and the claim-trace guard.

### S1.3 — Pricing recommendation (cc_sub)
**As the** Engine, **I want** to recommend a `cc_sub` price,
**so that** Setup can configure Stripe.
- AC: `product.price_amount_cents` + `price_interval` populated; editable by Owner. (trial/freemium unwired in v1.)

### S1.4 — Owner review/edit of strategy
**As the** Owner, **I want** to review and edit the brief + brand + price in the dashboard,
**so that** I keep strategic control before money/accounts get created.
- AC: edit + approve transitions `strategy → setup_ready`; Setup is blocked until approved.

---

## Epic 2 — Setup

### S2.1 — Templated landing site with funnel contract
**As the** Engine, **I want** to build a site from one maintained template, injecting AI copy + brand tokens, embedding the funnel-contract components (Stripe checkout, email capture, analytics, UTM capture),
**so that** each site is on-brand but every funnel is wired identically and cheap to QA.
- AC: site builds + static-exports + deploys to nginx under `marketing_domain`; the four contract components present + firing; layout/plumbing constant across products (no bespoke layout gen).

### S2.2 — Public funnel-ingest API (split)
**As the** Owner, **I want** the public visit/lead/Stripe-webhook endpoints separated from the private dashboard API,
**so that** public landing sites can record funnel events without exposing the operator API.
- AC: `/api/funnel/{slug}/visit`, `/.../lead`, `/api/stripe/webhook` are public, rate-limited, validated, CORS for the product origin; private API stays firewalled.

### S2.3 — Stripe configuration (cc_sub)
**As the** Engine, **I want** to create a Stripe product/price for `cc_sub`,
**so that** the funnel can take real subscriptions.
- AC: `stripe_price_id` stored on product; Checkout completes a **test-mode** subscription end-to-end and passes `client_reference_id` for attribution.

### S2.4 — Email capture + welcome email
**As the** Engine, **I want** to capture leads and send one welcome email,
**so that** leads are stored and acknowledged automatically.
- AC: lead row written on capture; one welcome email sends via SMTP/free ESP; drip explicitly deferred.

### S2.5 — Attribution chain
**As the** Operator, **I want** revenue traceable to the channel/content that drove it,
**so that** metrics tell me what actually works.
- AC: UTM per published link → first-touch cookie → `lead.first_touch_token` → Stripe `client_reference_id` → webhook join → `metric_event(stage=paid, channel_id, content_item_id)`.

### S2.6 — Channel accounts + human setup checklist
**As the** Operator, **I want** the engine to prep channel accounts and hand me an ordered checklist for the human-only steps,
**so that** I do only the irreducibly-human parts.
- AC: per enabled channel, engine lists exactly what the human must do (CAPTCHA account, OAuth consent, ToS, DNS, **SPF/DKIM/DMARC**, Stripe/banking) + a warm-up note before links go out; OAuth connect flows store tokens in the vault; checklist tracks completion.

### S2.7 — Pre-QA funnel smoke test
**As the** Engine, **I want** to auto-test each generated site before the human QA gate,
**so that** broken plumbing never reaches a human and QA stays cheap.
- AC: asserts build succeeds + the four funnel events fire + Checkout hits the correct test price; failure keeps the product in `setup_done` (never reaches `qa`).

### S2.8 — Launch checklist emission
**As the** Engine, **I want** to emit a pre-launch checklist from the actual setup output,
**so that** the QA gate has something concrete to verify.
- AC: checklist generated from real setup state; state → `setup_done` → `qa`.

---

## Epic 3 — QA gate

### S3.1 — Generate click-through checklist
**As the** Engine, **I want** to generate a concrete "open X, click Y, verify Z" checklist for product + funnel,
**so that** a non-technical tester can verify everything works.
- AC: steps concrete + ordered; cover product login/use AND the payment funnel (plumbing already smoke-tested, so the human verifies product + design/content).

### S3.2 — Record pass/fail + block go-live
**As the** QA tester, **I want** to mark each item pass/fail with comments,
**so that** failures are captured and block launch.
- AC: go-live blocked until all blocking items pass; failures visible to Operator; state → `live` only on full pass.

---

## Epic 4 — Crank core (Phase A: text/social + SEO blog)

### S4.1 — Scheduled crank
**As the** Engine, **I want** APScheduler to trigger generation per product/autonomous-channel on a configurable cadence,
**so that** content flows without anyone starting it.
- AC: default weekly batch; cadence configurable per product; a tick creates a `crank` `job_run` that fans out per enabled autonomous channel × content type; in-process worker loop retries failures.

### S4.2 — Text/social + SEO blog generation (with novelty)
**As the** Engine, **I want** to generate social posts and SEO blog articles from the brief + brand,
**so that** active channels have on-brand, non-repetitive content.
- AC: posts + articles produced with metadata, referencing content pillars; recent published items fed into the prompt to avoid near-duplicates.

### S4.3 — Critic + safety quality gate (one LLM call)
**As the** Engine, **I want** a single critic call returning `{score, safety_pass, notes}` on a different model tier than the generator,
**so that** only good, safe content proceeds — without doubling AI round-trips.
- AC: `score < threshold` → regenerate (max N) or skip+log; `safety_pass=false` → hard block (`guard_failed`); scores/notes persisted.

### S4.4 — Deterministic guard
**As the** Owner, **I want** a non-LLM guard (blocklist/regex + claim-traces-to-brief) on every item,
**so that** a hallucinated-but-on-brand post can't reach my real accounts.
- AC: items hitting the blocklist or making a claim not traceable to the brief/product facts are hard-blocked + logged, independent of the LLM critic.

### S4.5 — Publish adapters (blog + Reddit, idempotent + paced)
**As the** Engine, **I want** API-first adapters for the owned blog and Reddit with idempotency and pacing,
**so that** content reaches active channels autonomously without double-posts or spam bursts.
- AC: blog (file/API write) + Reddit (PRAW) publish; idempotent on `idempotency_key` (check remote before re-post); `scheduled_for` spread across the window with a per-channel `daily_cap`; results recorded; transient failures retry. (No browser fallback; IG/X/YouTube deferred.)

### S4.6 — Per-channel kill switch
**As the** Operator, **I want** to pause any product/channel instantly,
**so that** I can stop autonomous posting if something goes wrong.
- AC: pause halts new publishes within one cycle (checked immediately before publish); resume restores schedule.

### S4.7 — Retract a published item
**As the** Operator, **I want** to delete a bad live post,
**so that** a guardrail miss doesn't stay public.
- AC: adapter `delete(external_url)` implemented; dashboard "retract" action sets `content_item.status = retracted` and removes the remote post where the API allows.

### S4.8 — OAuth refresh handling
**As the** Engine, **I want** to proactively refresh tokens and fail safe,
**so that** a dead token doesn't silently kill a channel mid-window.
- AC: tokens refreshed before expiry; on refresh failure → channel `failed`, its publishes halt, and an alert fires (S6.2).

### S4.9 — Async spot-check sampling
**As the** Operator, **I want** the first item per channel + a random 10% flagged for async review,
**so that** I keep oversight without approving every post.
- AC: flagged items appear in a review queue; flagging never blocks publishing; reviewing is optional/async.

---

## Epic 5 — Crank media (Phase B: video + podcast) — gated on compute decision

### S5.0 — Phase B infrastructure
**As the** Owner, **I want** Celery + Redis + Postgres + a GPU host introduced when media lands,
**so that** long, parallel media jobs don't outgrow the in-process loop.
- AC: queue + workers run media jobs with retries/visibility; SQLite→Postgres migration path exercised; GPU host provisioned (acknowledged new spend).

### S5.1 — Short-form video pipeline
**As the** Engine, **I want** to produce short-form videos (script + voice + visuals) on the same crank + gates,
**so that** a video channel (e.g. YouTube) gets content autonomously.
- AC: uses video-podcast-maker / manim / ElevenLabs; passes critic+safety + deterministic guard; publishes via YouTube API; long jobs retry without blocking other work.

### S5.2 — Podcast/audio pipeline
**As the** Engine, **I want** to produce audio episodes via the generator→critic pattern,
**so that** an audio channel is autonomous too.
- AC: episodes generated + gated + published; long jobs resumable.

---

## Epic 6 — Metrics & acceptance

### S6.1 — Attributed funnel + revenue dashboard
**As the** Operator, **I want** per-product funnel + revenue metrics attributable to channel/content,
**so that** I can see what's cash-flowing and why.
- AC: impressions → visits → signups → paid → revenue shown per product, joinable to the channel/content that drove each conversion.

### S6.2 — Heartbeat + alerts
**As the** Operator, **I want** a daily heartbeat digest and alerts on failure/zero-reach,
**so that** "unattended" is actually verifiable and silent failures surface.
- AC: daily digest (published/failed/reach per channel); alerts on repeated publish-fail, dead token, or zero-reach over a window (shadowban signal).

### S6.3 — Content calendar
**As the** Operator, **I want** to see the content calendar/history,
**so that** I can trust the crank is running and review the spot-check queue.
- AC: calendar shows generated / critic-passed / published / retracted + performance; spot-check items surfaced.

### S6.4 — Auto Author end-to-end (acceptance)
**As the** Owner, **I want** Auto Author taken fully through the engine,
**so that** we prove the done-state on a real product.
- AC (DoD): new product onboarded with **zero code changes**; full Strategy→Setup→QA→Crank cycle completes; crank runs autonomously **≥2 weeks** publishing to blog + Reddit with **attributed** metrics + non-zero reach confirmed by heartbeat; both human gates exercised.

---

## Cross-cutting (apply to every story)
- Tests: TDD, >85% coverage, integration tests use real services (no mocking; Stripe test mode).
- Secrets never logged; encrypted at rest; AI token cost logged per `job_run` and budget-capped per product.
- Two API surfaces: public funnel-ingest (rate-limited, CORS) vs. private firewalled dashboard.
- Deploy: check VPS port conflicts before binding; verify CORS before remote deploy; idempotent CI/CD.
- Autonomous actions are observable (logged + heartbeat) and reversible (kill switch + retract).
