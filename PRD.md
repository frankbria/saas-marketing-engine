# Product Requirements Document — SaaS Marketing Engine (SME)

*Version 0.2 (draft for review) · 2026-06-28 · Owner: Frank · Operator: Greg*
*Source: BRAINSTORM.md transcript (2026-06-27) + brainstorm decisions + multi-perspective design review (2026-06-28)*

---

## 0. Revision 0.2 — design-review outcomes (authoritative; supersedes conflicting text below)

A debate-style simplicity-vs-functionality review changed these. Where older sections disagree, this block wins.

**Subtractions (v1 leaner):**
- **Infra:** SQLite (WAL) + APScheduler + a `job_run` table — **not** Postgres/Celery/Redis/Flower. Celery + Postgres return **only in Phase B** when long media jobs make a real queue load-bearing.
- **Channels — owned-first:** blog + email list **fully autonomous** (zero-ToS, owned). **One** warmed, value-first social channel (**Reddit**) as a careful experiment. **X / Instagram / YouTube are deferred or human-assisted in v1** (cold accounts auto-posting promo links = shadowban/ban bait). Browser-automation fallback **dropped** from v1 (it only served X/IG).
- **Landing site:** one strong **template** with AI-written copy + brand tokens — **not** bespoke per-product layout generation (bespoke = the QA balloon PRD §12 warns about).
- **Monetization:** implement **`cc_sub` only** in v1; keep the enum column. `trial`/`freemium` are wired when a product needs them.
- **Email:** single welcome email, **not** a drip sequence. **Data model:** `brand_kit`/`pricing_plan` collapse to JSON/fields on `product` until multi-plan pricing is real.

**Additions (cheap correctness/safety, required for an honest DoD):**
- **API split (bug fix):** a **public, rate-limited funnel-ingest API** (visit/lead/Stripe webhook) separate from the **private** dashboard API. The public landing site cannot POST to a firewalled API.
- **Attribution chain:** UTM (per content/channel) → cookie (first-touch) → `lead` → Stripe `client_reference_id`/metadata → webhook join → `metric_event.channel_id`. Without it "metrics flowing" can't attribute revenue to a channel/content item.
- **Failure detection:** daily heartbeat digest + alerts on repeated publish-fail, dead OAuth token, or **zero-reach** (shadowban detection). This is what makes "unattended ≥2 weeks" verifiable (replaces Flower).
- **Idempotency + novelty:** idempotency key per (content_item, channel) to prevent retry double-posts; feed recent published items into the generator to enforce content novelty.
- **Retract:** adapter `delete()` + dashboard "retract" action (kill switch only stops *future* posts).
- **Pre-QA smoke test:** automated funnel-contract test on each generated site (builds + the 4 events fire + Checkout hits the right test price) before the human QA gate; human then QAs design/content only.
- **Guardrail (merged + hardened):** one LLM critic call returns `{score, safety_pass, notes}`; **plus** a non-LLM blocklist/regex guard and a check that every factual claim traces to the strategy brief; **plus** hold the first item per channel + random 10% for async human spot-check. Generator and critic use different model tiers.
- **Setup checklist:** SPF/DKIM/DMARC per product domain; per-channel rate pacing/spacing with per-platform caps; proactive OAuth refresh → on-fail disable channel + alert.

**Cost reality (resolves NFR-3/G6 honesty):** Claude Agent SDK tokens are treated as **real metered spend** with a **per-product monthly token budget + hard stop**. Phase B media (ElevenLabs/ACE-Step/video render) needs a **GPU the dev VPS lacks** → **Phase B stays text-only until a separate GPU/host decision**; that decision is acknowledged new spend, out of the "no new spend" claim.

---

## 1. Summary

The **SaaS Marketing Engine (SME)** is a single-owner system that takes a finished,
production-ready SaaS codebase as input and **stands up and autonomously operates the
marketing and monetization around it** — landing site, payment funnel, multi-channel
content, and metrics — with humans involved only at two narrow gates.

It is the missing back-half of the owner's build process: today projects reach
"code complete, never tested, never marketed" and stall. SME closes that gap and turns
finished code into a cash-flowing business with the owner mostly out of the loop.

**It is not** a multi-tenant SaaS, a social network, or an ad-buying platform. All
products belong to the same owner, so there is **no tenant auth, no account isolation,
no per-customer security boundary** to build. "Generic over products" — yes. "Generic
over customers" — explicitly out of scope.

---

## 2. Goals & non-goals

### Goals
- G1. Run **multiple of the owner's products** through one engine, each as a swappable unit (config + workspace + credentials).
- G2. Automate the three stages the owner identified: **Strategy → Setup → Crank**.
- G3. Keep the **crank fully autonomous** — content generates, passes an AI quality gate, and publishes on a schedule with no per-post human approval.
- G4. Confine humans to two gates: **(a) account/payment/domain setup** and **(b) pre-launch QA click-through**.
- G5. Give the operator (Greg) a **dashboard** to onboard products, watch the funnel, and run the cycle without touching code.
- G6. **No new recurring spend** — run on the existing VPS and AI subscriptions.
- G7. **Zero product-specific hardcoding.** The engine is generic over products. Auto Author is the **first fixture/use case only** — it is treated identically to any future product. Everything product-specific (repo, domain, brand, ICP, channels, pricing, credentials) lives in the **Product record / config**, never in engine code. A future product must onboard with no code changes (this is also DoD-1). Auto Author may appear only as test-fixture/example data and the acceptance demo — never as a branch in business logic.

### Non-goals (v1)
- N1. B2E / enterprise marketing (long sales cycle, salesperson-in-loop). Deferred.
- N2. Paid advertising (transcript "stage 4"). Deferred.
- N3. Multi-tenant SaaS, customer logins, billing for *other people's* products.
- N4. Building the SaaS products themselves — SME consumes finished codebases as input.
- N5. A social platform / Product-Hunt competitor.

---

## 3. Personas

| Persona | Role | Relationship to SME |
|---|---|---|
| **Owner (Frank)** | Builds the products, sets direction, approves strategy briefs. | Wants to *stop* at "code done" and have SME take it from there. |
| **Operator (Greg)** | Runs the engine day-to-day, performs the two human gates, onboards new products. | Primary dashboard user. Eventually full-time. |
| **QA tester** | Greg or a ~$30/hr Upwork contractor. | Executes the AI-generated click-through checklist at the QA gate. |
| **The Engine** | Autonomous system. | The "third employee" — generates strategy, builds funnels, cranks content. |
| **End customer** *(indirect)* | Buyer of a product SME markets (e.g. Auto Author user). | Never touches SME; experiences only the generated funnel. |

---

## 4. Core concept: the Product unit

A **Product** is the first-class unit. Everything in SME is parameterized by it.

A Product record holds:
- **Identity & source**: name, codebase location (repo URL / path), one-paragraph description.
- **Brand kit** (generated in Strategy, editable): name/voice/visual seeds, tone.
- **Strategy brief** (generated): ICP, pain points, positioning, channel plan, content pillars, pricing recommendation.
- **Monetization config**: model (CC-upfront subscription | free trial | freemium) + price points + Stripe linkage.
- **Channels**: which of {landing site, IG, X, Reddit, YouTube} are active + their credentials.
- **Workspace**: generated assets (site, content calendar, media).
- **Credentials vault**: per-product encrypted API tokens / OAuth / Stripe keys.
- **State**: lifecycle stage (draft → strategy → setup → QA → live → paused) + metrics.

---

## 5. The pipeline

```
 [Product input: finished codebase + description]
        │
   ┌────▼─────┐   AI analyzes code + market →
   │ STRATEGY │   Marketing Brief + Brand Kit + pricing rec     (owner reviews in dashboard)
   └────┬─────┘
   ┌────▼─────┐   Provision funnel: bespoke landing site (std funnel contract),
   │  SETUP   │   Stripe, email list, social accounts, analytics, publishing creds
   └────┬─────┘   → emits launch checklist
   ┌────▼─────┐
   │ QA GATE  │   HUMAN: run AI checklist (log in, click, verify product + funnel)   ← gate
   └────┬─────┘   pass → go live
   ┌────▼─────┐   Scheduled, autonomous:
   │  CRANK   │   generate content → critic quality gate + brand-safety → publish
   └────┬─────┘   (API-first, browser fallback) → record metrics      [loops forever]
        │
   [Live, cash-flowing business + metrics dashboard]
```

**Human gates (the only two):**
1. **Account / payment / domain setup** (in Setup) — CAPTCHA-gated account creation, OAuth consent, platform ToS acceptance, Stripe/banking, domain + DNS. The engine prepares everything and hands the human a minimal, ordered to-do.
2. **Pre-launch QA** (QA gate) — human executes the engine-generated click-through checklist; dashboard records pass/fail; failures block go-live.

Everything else — including each individual piece of published content — is autonomous.

---

## 6. Functional requirements by module

### 6.1 Product Registry & Onboarding
- FR-1. Operator can register a product via a dashboard form (name, repo location, description, target monetization model).
- FR-2. Each product gets an isolated workspace + credentials vault (encrypted at rest).
- FR-3. Product lifecycle state is tracked and visible (draft → strategy → setup → QA → live → paused).

### 6.2 Strategy module
- FR-4. Ingests the product's codebase + description and produces a **Marketing Brief**: ICP/audience, pain points, positioning, channel plan, content pillars, suggested cadence.
- FR-5. Produces a **Brand Kit**: name/voice/visual direction seeds.
- FR-6. Produces a **pricing recommendation** mapped to the chosen monetization model.
- FR-7. Brief + brand kit are reviewable and editable by the owner in the dashboard before Setup proceeds.

### 6.3 Setup module
- FR-8. Generates a **bespoke landing/mini-site** per product that embeds the **standardized funnel contract** components: Stripe checkout, email capture, analytics snippet, conversion events. (Bespoke design, standard plumbing.)
- FR-9. Configures **Stripe** products/prices for the product's monetization model (CC-upfront sub / trial / freemium).
- FR-10. Sets up an **email list** + capture + welcome sequence.
- FR-11. Prepares **social accounts** and publishing credentials for active channels; produces a **human checklist** for the steps requiring CAPTCHA/OAuth/ToS/banking/DNS.
- FR-12. Wires **analytics** for the full funnel (impression → site → signup → paid).
- FR-13. Emits a **launch checklist** for the QA gate.

### 6.4 QA gate
- FR-14. Engine generates a concrete click-through checklist ("open X, click Y, verify Z") covering the product and the funnel.
- FR-15. Operator/tester records pass/fail per item in the dashboard; comments allowed.
- FR-16. Go-live is **blocked** until all blocking checklist items pass.

### 6.5 Crank module (autonomous)
- FR-17. Celery-beat schedule triggers content generation per product per channel on a configurable cadence (default weekly batch).
- FR-18. **Content pipelines** (phased): Phase A — text/social posts + SEO blog articles. Phase B — short-form video + podcast/audio. Reuse existing skills (video-podcast-maker, ElevenLabs/ACE-Step music, manim, podcast-studio-hub critic pattern).
- FR-19. **Quality gate**: generator → critic (separate AI pass) scores each item; below threshold → regenerate or skip. Mirrors the podcast-studio-hub pattern.
- FR-20. **Brand-safety guardrail**: every item checked against brand voice + a safety/compliance check before it can publish (own reputation/accounts at stake).
- FR-21. **Publish**: API-first per platform (YouTube Data, Reddit, X, blog/CMS); **browser-automation fallback** (web-ctl/Playwright) only where no API exists.
- FR-22. Records publish results + per-item metrics; retries failed jobs (Celery).
- FR-23. No per-post human approval — quality gate + guardrail are the only checks.

### 6.6 Metrics & dashboard
- FR-24. Funnel metrics per product: impressions/reach → site visits → signups → paying subscribers → revenue.
- FR-25. Content calendar + history (generated, approved-by-critic, published, performance).
- FR-26. Job/queue health (Celery/Flower) surfaced for the operator.
- FR-27. Per-product overview + portfolio roll-up.

### 6.7 Credentials & secrets
- FR-28. Per-product encrypted storage of API tokens, OAuth refresh tokens, Stripe keys.
- FR-29. OAuth connect flows for each platform, completed by the human at the setup gate.

---

## 7. Non-functional requirements
- NFR-1. **No multi-tenancy** — single owner; skip tenant auth/isolation. **The dashboard is a VPS-firewalled internal tool with no auth in v1** (bind to localhost/private interface; access via SSH tunnel or IP allowlist). Add a single operator login later only if exposed publicly.
- NFR-2. **Runs on the existing Hostinger dev VPS**; check port conflicts before binding; verify CORS (dashboard ↔ API) before first remote deploy.
- NFR-3. **Minimize new spend.** No new *infra* services beyond the existing VPS. AI tokens (Claude Agent SDK) are real metered spend, capped by a **per-product monthly token budget with a hard stop**. Phase B media compute (GPU) is acknowledged future spend, decided separately. *(per §0)*
- NFR-4. Long media jobs are **resumable/retryable** (Celery) and must not block the crank for other products/channels.
- NFR-5. Secrets encrypted at rest; never logged.
- NFR-6. Autonomous publishing must be **pausable per product/channel** instantly from the dashboard (kill switch).
- NFR-7. >85% test coverage on engine logic; integration tests use real services (no mocking) per house standard.

---

## 8. Tech stack
- **Backend**: FastAPI (Python, `uv`).
- **Frontend/dashboard**: Next.js (Nova template — gray palette, Hugeicons, Nunito Sans), Shadcn/UI + Tailwind. No auth in v1 (VPS-firewalled internal tool).
- **DB**: SQLite (WAL) for v1 → Postgres when Phase B load demands it. *(per §0)*
- **Queue/scheduler**: APScheduler + a `job_run` table for v1; Celery + Redis (+ Flower) return in **Phase B** for long media jobs. *(per §0)*
- **AI**: Claude Agent SDK (latest Claude models) for strategy, generation, and critic passes.
- **Content gen**: existing skills/tools — video-podcast-maker, ElevenLabs music / ACE-Step, manim, podcast-studio-hub generator→critic pattern.
- **Publishing**: platform APIs (YouTube Data, Reddit, X, blog/CMS) + browser automation fallback (web-ctl / Playwright).
- **Payments**: Stripe (subscriptions; configurable model per product).
- **Analytics**: lightweight funnel + revenue tracking (self-hosted, no new spend).
- **Deploy**: Hostinger dev VPS (195.35.14.177); feature branches → PR to main; pre-commit hooks; idempotent CI/CD.

---

## 9. Development phases (→ map to GitHub issues)

**Phase 0 — Foundation**
Repo scaffold (FastAPI + Next.js Nova monorepo), Postgres + Redis + Celery + Flower, CI/CD,
VPS deploy with port-conflict + CORS checks, secrets vault, Product registry model + onboarding form, single operator login.

**Phase 1 — Strategy**
Codebase ingest → Marketing Brief + Brand Kit + pricing recommendation; dashboard review/edit.

**Phase 2 — Setup**
Bespoke landing-site generation with the standardized funnel contract; Stripe (configurable models);
email capture + welcome sequence; analytics wiring; credential/OAuth connect flows; human-step checklist; launch checklist.

**Phase 3 — QA gate**
Checklist generation, pass/fail tracking, go-live block.

**Phase 4 — Crank core (Phase A content)**
Celery-beat scheduler, generator→critic quality gate, brand-safety guardrail, publish (API-first + browser fallback), metrics recording, per-channel kill switch. Content: **text/social + SEO blog**.

**Phase 5 — Crank media (Phase B content)**
Short-form video + podcast/audio pipelines on the same crank.

**Phase 6 — Metrics & acceptance**
Funnel + revenue dashboard, portfolio roll-up; **onboard Auto Author end-to-end** to satisfy the done-state.

> Realistic note: Phases 0–4 + 6 (with Phase A content) hit the "operator runs it unattended" milestone within the ~1-month target. Phase 5 (video/podcast) is a fast-follow and may slip past the month — acceptable.

---

## 10. Success criteria (definition of done)
- **DoD-1 (primary).** The operator can onboard a **brand-new product** and run the full **Strategy → Setup → QA → Crank** cycle **with zero code changes**.
- **DoD-2.** The crank runs **autonomously for ≥2 weeks**, publishing quality-gate-passed content on schedule across active channels, with metrics flowing to the dashboard.
- **DoD-3.** **Auto Author** is live through the engine: landing + payment funnel live, accounts connected, crank publishing.
- **DoD-4.** Both human gates work: a tester can complete a generated QA checklist, and the setup checklist covers every human-required step.

---

## 11. Resolved inputs (Auto Author, product #0)
- **I-1. ✓** Auto Author — `github.com/frankbria/auto-author` (local: `~/projects/auto-author`). AI app for writing long-form **non-fiction books**: AI-generated TOC from a summary, interview-style chapter prompts, voice/text input, AI draft generation in multiple styles, rich-text editing, PDF/DOCX export. Stack: Next.js + FastAPI + MongoDB + better-auth + OpenAI. **Likely ICP**: coaches, consultants, founders, thought-leaders writing a book for authority/lead-gen. Clear B2C/small-business subscription.
- **I-2. ✓** No marketing accounts exist yet — engine creates all social/channel accounts from scratch (human completes CAPTCHA/OAuth/ToS at the setup gate).
- **I-3. ✓** No existing brand → generate fresh in Strategy. **Domain `autoauthor.app` exists**; `dev.autoauthor.app` is the build/staging site. The marketing landing site targets the production domain; keep it distinct from the app's staging host.
- **I-4. ✓** Dashboard = **VPS-firewalled internal tool, no auth in v1** (see NFR-1).

## 12. Open questions / risks
- Platform ToS for autonomous posting on owned accounts (esp. X/IG) — may force more browser-fallback or human-assisted posting on some channels.
- Bespoke-per-product sites raise QA cost; the standardized funnel contract must stay strict or QA balloons.
- "All four content types in ~1 month" is optimistic; Phase B is the planned spill-over.
