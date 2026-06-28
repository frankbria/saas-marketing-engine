# User Stories — SaaS Marketing Engine (SME)

*Version 0.1 (draft) · 2026-06-28 · Companion to PRD.md*

Personas: **Owner** (Frank), **Operator** (Greg), **QA tester**, **Engine** (autonomous).
Stories are grouped by epic = development phase. Each is sized to become one or a few atomic
GitHub issues. Acceptance criteria are written to be directly testable.

---

## Epic 0 — Foundation

### S0.1 — Monorepo scaffold
**As the** Owner, **I want** a FastAPI + Next.js (Nova) monorepo with pre-commit hooks and CI,
**so that** every later module lands in a consistent, tested structure.
- AC: `uv` backend + Next.js Nova frontend in one repo; pre-commit (ruff/black/lint) passes; CI runs tests on PR; feature-branch → PR → main flow documented.

### S0.2 — Infra services up
**As the** Operator, **I want** Postgres, Redis, Celery (worker + beat), and Flower running locally and on the VPS,
**so that** state and the job queue exist before any module needs them.
- AC: `docker compose` (or VPS service files) brings all services up; Flower reachable; a trivial Celery task round-trips; **VPS port conflicts checked and documented before binding**.

### S0.3 — Single operator login
**As the** Operator, **I want** one login to the dashboard (BetterAuth),
**so that** the internal tool isn't world-open, without building multi-user/tenant auth.
- AC: one operator account; protected dashboard routes; no per-product user separation (per non-goal N3).

### S0.4 — Product registry + onboarding form
**As the** Operator, **I want** to register a product (name, repo location, description, monetization model) via a form,
**so that** the engine has a unit to operate on.
- AC: Product persists to Postgres with isolated workspace + empty credentials vault; lifecycle state = `draft`; product appears in a portfolio list.

### S0.5 — Encrypted credentials vault
**As the** Owner, **I want** per-product secrets encrypted at rest and never logged,
**so that** API tokens and Stripe keys are safe on a shared VPS.
- AC: write/read secret round-trips; ciphertext at rest; secrets absent from logs; CORS verified before remote deploy.

---

## Epic 1 — Strategy

### S1.1 — Codebase ingest → Marketing Brief
**As the** Engine, **I want** to analyze a product's codebase + description and produce a Marketing Brief,
**so that** setup and content have a strategy to follow.
- AC: brief includes ICP/audience, pain points, positioning, channel plan, content pillars, suggested cadence; persisted to the Product; generated via Claude Agent SDK.

### S1.2 — Brand Kit generation
**As the** Engine, **I want** to produce a brand kit (name/voice/visual seeds),
**so that** every downstream asset is on-brand.
- AC: brand kit persisted + linked to the Product; includes tone/voice descriptors usable by the crank's brand-safety check.

### S1.3 — Pricing recommendation
**As the** Engine, **I want** to recommend pricing mapped to the chosen monetization model,
**so that** Setup can configure Stripe.
- AC: recommendation covers CC-upfront sub / trial / freemium variants for the selected model; editable by Owner.

### S1.4 — Owner review/edit of strategy
**As the** Owner, **I want** to review and edit the brief + brand kit + pricing in the dashboard,
**so that** I keep strategic control before money/accounts get created.
- AC: edit + approve transitions product state `strategy → setup-ready`; Setup is blocked until approved.

---

## Epic 2 — Setup

### S2.1 — Bespoke landing site with funnel contract
**As the** Engine, **I want** to generate a bespoke landing/mini-site that embeds the standardized funnel components (Stripe checkout, email capture, analytics, conversion events),
**so that** each product looks distinct but every funnel is wired identically.
- AC: site builds + deploys to VPS; the four funnel components present and firing on every generated site; documented "funnel contract" interface.

### S2.2 — Stripe configuration (configurable model)
**As the** Engine, **I want** to create Stripe products/prices for the product's monetization model,
**so that** the funnel can take real payments.
- AC: supports CC-upfront subscription, free trial, and freemium; checkout completes a test-mode subscription end-to-end.

### S2.3 — Email list + welcome sequence
**As the** Engine, **I want** to set up an email list, capture, and welcome sequence,
**so that** leads are captured and nurtured automatically.
- AC: capture stores contacts; welcome email sends on signup.

### S2.4 — Social accounts + human setup checklist
**As the** Operator, **I want** the engine to prepare social/publishing accounts and hand me an ordered checklist for the CAPTCHA/OAuth/ToS/banking/DNS steps,
**so that** I do only the irreducibly-human parts.
- AC: per active channel, engine lists exactly what the human must do; OAuth connect flows store tokens in the vault; checklist tracks completion.

### S2.5 — Analytics wiring
**As the** Operator, **I want** the full funnel instrumented (impression → site → signup → paid),
**so that** the dashboard can show whether it's working.
- AC: events recorded at each funnel stage and queryable per product; no new paid analytics service.

### S2.6 — Launch checklist emission
**As the** Engine, **I want** to emit a pre-launch checklist,
**so that** the QA gate has something concrete to verify.
- AC: checklist generated from the actual setup output; product state → `qa`.

---

## Epic 3 — QA gate

### S3.1 — Generate click-through checklist
**As the** Engine, **I want** to generate a concrete "open X, click Y, verify Z" checklist for product + funnel,
**so that** a non-technical tester can verify everything works.
- AC: steps are concrete + ordered; cover product login/use AND the payment funnel.

### S3.2 — Record pass/fail + block go-live
**As the** QA tester, **I want** to mark each item pass/fail with comments,
**so that** failures are captured and block launch.
- AC: go-live blocked until all blocking items pass; failures visible to Operator; state → `live` only on full pass.

---

## Epic 4 — Crank core (Phase A: text/social + SEO blog)

### S4.1 — Scheduled crank
**As the** Engine, **I want** Celery beat to trigger content generation per product/channel on a configurable cadence,
**so that** content flows without anyone starting it.
- AC: default weekly batch; cadence configurable per product; jobs enqueued + retried on failure.

### S4.2 — Text/social + SEO blog generation
**As the** Engine, **I want** to generate social posts and SEO blog articles from the brief + brand kit,
**so that** the active channels have on-brand content.
- AC: posts for active social channels + blog articles produced and stored with metadata; references content pillars from the brief.

### S4.3 — Generator → critic quality gate
**As the** Engine, **I want** a separate AI critic to score each item and reject/regenerate below threshold,
**so that** only good content publishes (no garbage).
- AC: critic score persisted; below-threshold items regenerate or are skipped (logged); mirrors podcast-studio-hub pattern.

### S4.4 — Brand-safety guardrail
**As the** Owner, **I want** every item checked against brand voice + a safety/compliance check before publish,
**so that** autonomous posting never damages my reputation/accounts.
- AC: items failing the guardrail are blocked + logged; guardrail uses the brand kit voice descriptors.

### S4.5 — Publish (API-first, browser fallback)
**As the** Engine, **I want** to publish via official APIs where available and browser automation only where not,
**so that** content reaches each channel autonomously.
- AC: API publishers for YouTube/Reddit/X/blog; browser fallback path for channels lacking an API; publish results recorded; failed publishes retried.

### S4.6 — Per-channel kill switch
**As the** Operator, **I want** to pause any product/channel instantly,
**so that** I can stop autonomous posting if something goes wrong.
- AC: pause halts new publishes within one cycle; resume restores schedule.

---

## Epic 5 — Crank media (Phase B: video + podcast)

### S5.1 — Short-form video pipeline
**As the** Engine, **I want** to produce short-form videos (script + voice + visuals) on the same crank + quality gate,
**so that** YouTube/Reels channels get content autonomously.
- AC: uses video-podcast-maker / manim / ElevenLabs; passes generator→critic + brand-safety; publishes via YouTube API; long jobs retry without blocking other work.

### S5.2 — Podcast/audio pipeline
**As the** Engine, **I want** to produce audio episodes via the generator→critic pattern,
**so that** the audio channel is autonomous too.
- AC: episodes generated + quality-gated + published; long jobs resumable.

---

## Epic 6 — Metrics & acceptance

### S6.1 — Funnel + revenue dashboard
**As the** Operator, **I want** per-product funnel and revenue metrics,
**so that** I can see whether a product is cash-flowing.
- AC: impressions → visits → signups → paid → revenue shown per product; portfolio roll-up across products.

### S6.2 — Content calendar + job health
**As the** Operator, **I want** to see the content calendar/history and Celery/Flower job health,
**so that** I can trust the crank is running.
- AC: calendar shows generated/critic-passed/published + performance; queue health surfaced.

### S6.3 — Auto Author end-to-end (acceptance)
**As the** Owner, **I want** Auto Author taken fully through the engine,
**so that** we prove the done-state on a real product.
- AC (DoD): new product onboarded with **zero code changes**; full Strategy→Setup→QA→Crank cycle completes; crank runs autonomously **≥2 weeks** with metrics flowing; both human gates exercised.

---

## Cross-cutting (apply to every story)
- Tests: TDD, >85% coverage, integration tests use real services (no mocking).
- Secrets never logged; encrypted at rest.
- Deploy: check VPS port conflicts before binding; verify CORS before remote deploy; idempotent CI/CD.
- Autonomous actions must be observable (logged) and reversible (kill switch / pause).
