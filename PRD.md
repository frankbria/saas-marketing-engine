# Product Requirements Document — SaaS Marketing Engine (SME)

*Version 0.2 · 2026-06-28 · Owner: Frank · Operator: Greg*
*Source: BRAINSTORM.md (2026-06-27) + brainstorm decisions + multi-perspective design review (2026-06-28)*
*Changelog v0.2: leaner v1 infra (SQLite + APScheduler); owned-first channels; cc_sub-only; templated site; merged+hardened guardrail; added public/private API split, revenue attribution, failure detection, idempotency, retract, pre-QA smoke test; honest cost model.*

---

## 1. Summary

The **SaaS Marketing Engine (SME)** is a single-owner system that takes a finished,
production-ready SaaS codebase as input and **stands up and autonomously operates the
marketing and monetization around it** — landing site, payment funnel, content across owned
and (carefully) social channels, and metrics — with humans involved only at two narrow gates.

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
- G3. Keep the **crank autonomous** — content generates, passes an AI + deterministic quality gate, and publishes on a schedule with no per-post human approval (a small random sample is spot-checked async, never blocking).
- G4. Confine humans to two gates: **(a) account/payment/domain setup** and **(b) pre-launch QA click-through**.
- G5. Give the operator (Greg) a **dashboard** to onboard products, watch the funnel, and run the cycle without touching code.
- G6. **Minimize new spend** — no new infra services beyond the existing VPS. AI tokens are real metered spend, capped per product (see NFR-3); Phase B media GPU is acknowledged future spend decided separately.
- G7. **Zero product-specific hardcoding.** The engine is generic over products. Auto Author is the **first fixture/use case only** — treated identically to any future product. Everything product-specific (repo, domain, brand, ICP, channels, pricing, credentials) lives in the **Product record / config**, never in engine code. A future product must onboard with no code changes (also DoD-1). Auto Author may appear only as test-fixture/example data and the acceptance demo — never as a branch in business logic.

### Non-goals (v1)
- N1. B2E / enterprise marketing (long sales cycle, salesperson-in-loop). Deferred.
- N2. Paid advertising (transcript "stage 4"). Deferred.
- N3. Multi-tenant SaaS, customer logins, billing for *other people's* products.
- N4. Building the SaaS products themselves — SME consumes finished codebases as input.
- N5. A social platform / Product-Hunt competitor.
- N6. Autonomous posting to X / Instagram / YouTube. Deferred or human-assisted in v1 (see §6.5).
- N7. Video / podcast generation (Phase B; needs GPU not on the current box).

---

## 3. Personas

| Persona | Role | Relationship to SME |
|---|---|---|
| **Owner (Frank)** | Builds the products, sets direction, approves strategy briefs. | Wants to *stop* at "code done" and have SME take it from there. |
| **Operator (Greg)** | Runs the engine day-to-day, performs the two human gates, onboards new products, does async content spot-checks. | Primary dashboard user. Eventually full-time. |
| **QA tester** | Greg or a ~$30/hr Upwork contractor. | Executes the AI-generated click-through checklist at the QA gate. |
| **The Engine** | Autonomous system. | The "third employee" — generates strategy, builds funnels, cranks content. |
| **End customer** *(indirect)* | Buyer of a product SME markets (e.g. Auto Author user). | Never touches SME; experiences only the generated funnel. |

---

## 4. Core concept: the Product unit

A **Product** is the first-class unit. Everything in SME is parameterized by it.

A Product record holds:
- **Identity & source**: name, codebase location (repo URL / path), one-paragraph description.
- **Brand kit** (generated in Strategy, editable; stored as JSON on the product): name/voice/visual seeds, tone.
- **Strategy brief** (generated): ICP, pain points, positioning, channel plan, content pillars, cadence.
- **Monetization config**: model enum (`cc_sub` | `trial` | `freemium`) + price fields + Stripe linkage. **v1 implements `cc_sub`** (CC-upfront subscription); the other branches wire in when a product needs them.
- **Channels**: which channels are active + their credentials. v1 channel set = **blog (owned)** + **email** + **Reddit**; IG/X/YouTube are config-present but deferred/human-assisted.
- **Workspace**: generated assets (site, content calendar, media).
- **Credentials vault**: per-product encrypted API tokens / OAuth / Stripe keys.
- **State**: lifecycle stage (draft → strategy → setup → qa → live → paused) + metrics.

---

## 5. The pipeline

```
 [Product input: finished codebase + description]
        │
   ┌────▼─────┐   AI analyzes code + market →
   │ STRATEGY │   Marketing Brief + Brand Kit + cc_sub pricing rec   (owner reviews in dashboard)
   └────┬─────┘
   ┌────▼─────┐   Provision funnel: templated landing site (funnel contract: Stripe/email/
   │  SETUP   │   analytics/UTM), email list, channel creds, attribution; auto smoke-test;
   └────┬─────┘   emits human setup checklist + launch checklist
   ┌────▼─────┐
   │ QA GATE  │   HUMAN: run AI checklist (log in, click, verify product + funnel)   ← gate
   └────┬─────┘   pass → go live
   ┌────▼─────┐   Scheduled (weekly), autonomous, per channel:
   │  CRANK   │   generate → critic+safety (1 LLM call) + deterministic guard →
   └────┬─────┘   publish (API-first: owned blog + Reddit) → record metrics → heartbeat
        │          (idempotent; novelty-aware; paced; random 10% async spot-check)
   [Live, cash-flowing business + attributed metrics dashboard]
```

**Human gates (the only two):**
1. **Account / payment / domain setup** (in Setup) — CAPTCHA-gated account creation, OAuth consent, platform ToS acceptance, Stripe/banking, domain + DNS (incl. SPF/DKIM/DMARC). The engine prepares everything and hands the human a minimal, ordered to-do.
2. **Pre-launch QA** (QA gate) — human executes the engine-generated click-through checklist; dashboard records pass/fail; failures block go-live. (Funnel *plumbing* is already auto-smoke-tested before this gate, so the human QAs product + design/content.)

Everything else is autonomous. Individual posts are not approved pre-publish; a random 10% + the first item per channel are spot-checked **asynchronously** (never blocking).

---

## 6. Functional requirements by module

### 6.1 Product Registry & Onboarding
- FR-1. Operator can register a product via a dashboard form (name, repo location, description, target monetization model).
- FR-2. Each product gets an isolated workspace + credentials vault (encrypted at rest).
- FR-3. Product lifecycle state is tracked and visible (draft → strategy → setup → qa → live → paused).

### 6.2 Strategy module
- FR-4. Ingests the product's codebase + description and produces a **Marketing Brief**: ICP/audience, pain points, positioning, channel plan, content pillars, suggested cadence.
- FR-5. Produces a **Brand Kit** (name/voice/visual seeds), stored as JSON on the product.
- FR-6. Produces a **pricing recommendation** for the `cc_sub` model.
- FR-7. Brief + brand kit are reviewable and editable by the owner in the dashboard before Setup proceeds.

### 6.3 Setup module
- FR-8. Generates a landing/mini-site from **one maintained template**, injecting AI-written copy + brand tokens. Every site embeds the **funnel contract** components: Stripe checkout, email capture, analytics snippet, conversion events, and per-channel **UTM** capture. (Standard plumbing, templated design → cheap, repeatable QA.)
- FR-9. Configures **Stripe** for the `cc_sub` model (product + price); Checkout in **test mode** until QA passes.
- FR-10. Sets up an **email list** + capture + **one welcome email** (drip deferred until list volume justifies it).
- FR-11. Prepares **channel accounts** and publishing credentials for active channels; produces a **human checklist** for the steps requiring CAPTCHA/OAuth/ToS/banking/DNS and **SPF/DKIM/DMARC** per product domain.
- FR-12. Wires **analytics + attribution** for the full funnel (impression → visit → signup → paid), with the attribution chain threaded (UTM → first-touch cookie → lead → Stripe `client_reference_id` → webhook).
- FR-13. **Public funnel-ingest API** (visit/lead/Stripe webhook) is provisioned separately from the private dashboard API (see NFR-1); rate-limited, validated, CORS for the product origin.
- FR-14. Runs an **automated pre-QA smoke test** on each generated site (build succeeds + the four funnel events fire + Checkout hits the correct test price). Failures block reaching the human QA gate.
- FR-15. Emits a **launch checklist** for the QA gate.

### 6.4 QA gate
- FR-16. Engine generates a concrete click-through checklist ("open X, click Y, verify Z") covering the product and the funnel.
- FR-17. Operator/tester records pass/fail per item in the dashboard; comments allowed.
- FR-18. Go-live is **blocked** until all blocking checklist items pass.

### 6.5 Crank module (autonomous)
- FR-19. **APScheduler** triggers content generation per product per active channel on a configurable cadence (default weekly batch); each run is recorded in a `job_run` row with retry handling.
- FR-20. **Content pipelines** (phased): Phase A — text/social posts + SEO blog articles. Phase B — short-form video + podcast/audio (deferred; needs GPU). Reuse existing skills (video-podcast-maker, ElevenLabs/ACE-Step, manim, podcast-studio-hub critic pattern) in Phase B.
- FR-21. **Channel set (v1)**: **blog** (owned site, file/API write — zero ToS risk, fully autonomous) + **Reddit** (PRAW; warmed, value-first, non-spam content policy). **IG / X / YouTube deferred or human-assisted** (cold accounts auto-posting promo links = shadowban/ban risk). No browser-automation fallback in v1.
- FR-22. **Quality gate**: one LLM **critic** call returns `{score, safety_pass, notes}`; below threshold → regenerate (max N) or skip+log; `safety_pass=false` → hard block. The generator and critic use **different model tiers**.
- FR-23. **Deterministic guard** (independent of the LLM): a blocklist/regex check + a check that every factual claim in an item traces to the strategy brief/product facts. Hard-blocks on failure.
- FR-24. **Novelty**: recent published items are fed into the generator to avoid near-duplicate/repetitive content (itself a spam signal).
- FR-25. **Publish** via API-first adapters; results + per-item metrics recorded. **Idempotency key** per (content_item, channel) prevents retry double-posts. **Pacing**: `scheduled_for` is spread across the cadence window with a per-platform daily cap.
- FR-26. **Retract**: each adapter supports `delete(external_url)`; the dashboard exposes a "retract" action (the kill switch only stops *future* posts).
- FR-27. **Spot-check**: hold the first item per channel + a random 10% for **async** human review; this never blocks publishing in steady state.
- FR-28. No per-post pre-publish human approval — the gates above are the only checks.

### 6.6 Metrics, observability & dashboard
- FR-29. **Attributed** funnel metrics per product: impressions/reach → visits → signups → paying subscribers → revenue, joinable to the channel/content item that drove each conversion.
- FR-30. Content calendar + history (generated, critic-passed, published, performance).
- FR-31. **Failure detection**: a daily **heartbeat digest** (published / failed / reach per channel) + alerts on repeated publish-fail, dead/expired OAuth token, or **zero-reach** (shadowban signal). This is what makes "unattended ≥2 weeks" verifiable.
- FR-32. Per-product overview; portfolio roll-up is deferred until >1 product runs.

### 6.7 Credentials & secrets
- FR-33. Per-product encrypted storage of API tokens, OAuth refresh tokens, Stripe keys.
- FR-34. OAuth connect flows for each platform, completed by the human at the setup gate; **proactive token refresh** — on refresh failure, mark the channel `failed`, halt its publishes, and fire an alert (FR-31).

---

## 7. Non-functional requirements
- NFR-1. **No multi-tenancy** — single owner; skip tenant auth/isolation. **Two API surfaces:** the **private** dashboard/operator API is firewalled with **no auth in v1** (bind private interface; SSH tunnel / IP allowlist); the **public** funnel-ingest API + generated landing sites are internet-facing via nginx, rate-limited, with CORS for the product origin.
- NFR-2. **Runs on the existing Hostinger dev VPS**; check port conflicts before binding; verify CORS before first remote deploy.
- NFR-3. **Minimize new spend.** No new infra services beyond the existing VPS. AI tokens (Claude Agent SDK) are real metered spend, capped by a **per-product monthly token budget with a hard stop**; usage logged per `job_run`. Phase B media compute (GPU) is acknowledged future spend, decided separately.
- NFR-4. Crank work for one product/channel must not block others; failed jobs retry. (v1: APScheduler + in-process worker loop + `job_run` retries. Phase B introduces Celery/Redis when long media jobs make a real queue load-bearing.)
- NFR-5. Secrets encrypted at rest; never logged.
- NFR-6. Autonomous publishing must be **pausable per product/channel** instantly from the dashboard (kill switch), checked immediately before each publish.
- NFR-7. >85% test coverage on engine logic; integration tests use real services (no mocking) per house standard.

---

## 8. Tech stack
- **Backend**: FastAPI (Python, `uv`) — two routers/surfaces (private dashboard API, public funnel-ingest API). APScheduler runs the crank in-process for v1.
- **Frontend/dashboard**: Next.js (Nova template — gray palette, Hugeicons, Nunito Sans), Shadcn/UI + Tailwind. No auth in v1 (VPS-firewalled internal tool).
- **DB**: SQLite (WAL) for v1 → Postgres when Phase B load demands it.
- **Queue/scheduler**: APScheduler + a `job_run` table for v1; Celery + Redis (+ Flower) return in **Phase B** for long media jobs.
- **AI**: Claude Agent SDK (latest Claude models) — generator and critic on different tiers; token usage budgeted + logged per product.
- **Content gen (Phase B)**: video-podcast-maker, ElevenLabs / ACE-Step, manim, podcast-studio-hub generator→critic pattern.
- **Publishing**: API-first — owned blog (file/API write) + Reddit (PRAW) in v1. No browser-automation fallback in v1.
- **Payments**: Stripe (`cc_sub` subscription; enum keeps trial/freemium for later).
- **Analytics**: self-hosted funnel + attribution events; revenue derived from Stripe webhooks. No paid analytics SaaS.
- **Deploy**: Hostinger dev VPS (195.35.14.177); nginx fronts public sites + public API; feature branches → PR to main; pre-commit hooks; idempotent CI/CD.

---

## 9. Development phases (→ map to GitHub issues)

**Phase 0 — Foundation**
Repo scaffold (FastAPI + Next.js Nova monorepo), SQLite + APScheduler + `job_run`, CI/CD,
VPS deploy with port-conflict + CORS checks, secrets vault, Product registry model + onboarding form. (No operator login — firewalled internal tool.)

**Phase 1 — Strategy**
Codebase ingest → Marketing Brief + Brand Kit (JSON) + `cc_sub` pricing recommendation; dashboard review/edit.

**Phase 2 — Setup**
Templated landing-site generation (AI copy + brand tokens) with the funnel contract + UTM; **public funnel-ingest API split**; Stripe (`cc_sub`, test mode); email capture + welcome email; analytics + **attribution chain**; credential/OAuth connect flows; human-step checklist (incl. SPF/DKIM/DMARC); **automated pre-QA smoke test**; launch checklist.

**Phase 3 — QA gate**
Checklist generation, pass/fail tracking, go-live block.

**Phase 4 — Crank core (Phase A content)**
APScheduler scheduler; generator → critic+safety (1 LLM call) + deterministic guard; novelty; publish (owned blog + Reddit, API-first); idempotency + pacing; metrics + **attribution**; **heartbeat + alerts**; per-channel kill switch; retract; async spot-check. Content: **text/social + SEO blog**.

**Phase 5 — Crank media (Phase B content)**
Short-form video + podcast pipelines on the same crank; introduce Celery/Redis/Postgres + GPU host. (Requires the separate compute-spend decision.)

**Phase 6 — Metrics & acceptance**
Attributed funnel + revenue dashboard; **onboard Auto Author end-to-end** to satisfy the done-state.

> Realistic note: Phases 0–4 + 6 (Phase A content) hit the "operator runs it unattended" milestone within the ~1-month target. Phase 5 (video/podcast) is a fast-follow gated on the GPU/compute decision and may slip — acceptable.

---

## 10. Success criteria (definition of done)
- **DoD-1 (primary).** The operator can onboard a **brand-new product** and run the full **Strategy → Setup → QA → Crank** cycle **with zero code changes**.
- **DoD-2.** The crank runs **autonomously for ≥2 weeks**, publishing quality-gate-passed content on schedule across active channels, with **attributed** metrics flowing and the heartbeat confirming non-zero reach.
- **DoD-3.** **Auto Author** is live through the engine: landing + payment funnel live, channels connected, crank publishing to blog + Reddit.
- **DoD-4.** Both human gates work: a tester can complete a generated QA checklist, and the setup checklist covers every human-required step.

---

## 11. Resolved inputs (Auto Author, product #0)
- **I-1. ✓** Auto Author — `github.com/frankbria/auto-author` (local: `~/projects/auto-author`). AI app for writing long-form **non-fiction books**: AI-generated TOC from a summary, interview-style chapter prompts, voice/text input, AI draft generation in multiple styles, rich-text editing, PDF/DOCX export. Stack: Next.js + FastAPI + MongoDB + better-auth + OpenAI. **Likely ICP**: coaches, consultants, founders, thought-leaders writing a book for authority/lead-gen. Clear B2C/small-business subscription.
- **I-2. ✓** No marketing accounts exist yet — engine creates channel accounts (human completes CAPTCHA/OAuth/ToS at the setup gate); cold accounts get a warm-up period before any links go out.
- **I-3. ✓** No existing brand → generate fresh in Strategy. **Domain `autoauthor.app` exists**; `dev.autoauthor.app` is the build/staging site. The marketing landing site targets the production domain; keep it distinct from the app's staging host.
- **I-4. ✓** Dashboard = **VPS-firewalled internal tool, no auth in v1** (see NFR-1).

## 12. Open questions / risks
- **Cold-account reach** is the top risk: new accounts auto-posting promo links get shadowbanned, and reach silently → 0. Mitigations: owned-first channels, warm-up, value-first Reddit policy, zero-reach alerting. Re-evaluate adding X/IG/YouTube only once accounts are warmed.
- **AI token cost** scales with products × channels × cadence × regenerations; the per-product budget + hard stop bounds it but needs monitoring once live.
- **Phase B compute**: video/podcast need a GPU host (new spend) — a separate decision before Phase 5.
- **Reddit ToS/subreddit rules** vary; the value-first content policy + per-subreddit targeting must be respected to avoid bans.
