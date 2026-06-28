# Technical Specification — SaaS Marketing Engine (SME)

*Version 0.1 (draft) · 2026-06-28 · Derives from PRD.md + USER_STORIES.md*

This spec is the implementation contract. It is concrete enough that each section maps to
dependency-ordered GitHub issues (see §13). Scope is **v1, single-owner**, with **Auto Author
as the first fixture** (not a special case).

> **Design rule (PRD G7): zero product-specific hardcoding.** No Auto-Author-specific branch
> may exist in engine code. Every product-specific value (repo, domain, brand, ICP, channels,
> pricing, credentials, cadence) lives in the **Product record / config**. Auto Author appears
> only as test-fixture data and the acceptance demo. Any new product must run end-to-end with
> zero code changes. Reviewers reject PRs that reference a specific product by name in logic.

## 0. Revision 0.2 deltas (authoritative — supersedes conflicting text below; see PRD §0)

- **Storage/scheduler:** SQLite (WAL) + APScheduler + `job_run` table for v1. Celery/Redis/Flower + Postgres return in Phase B. Replace all v1 references to Celery/beat/Redis/Flower accordingly; "enqueue a job" = insert a `job_run` row + APScheduler/worker-loop with a retry column.
- **API split:** two FastAPI surfaces — **public** funnel-ingest (`/api/funnel/...` visit/lead, `/api/stripe/webhook`), rate-limited + narrowly validated + CORS for the product origin; **private** dashboard/operator API on the firewalled interface. NFR-1's no-auth applies only to the private surface.
- **Data model:** fold `brand_kit`→`product.brand_json` and `pricing_plan`→fields on `product` (amount_cents, interval, stripe_price_id) for v1. Add to `content_item`: `idempotency_key`, `tracking_token` (UTM), `attribution_channel_id`. Add `lead` table with `first_touch_token`. Implement `monetization_model = cc_sub` only.
- **Channels:** v1 = blog (owned, file/API write, autonomous) + Reddit (PRAW, warmed/value-first, autonomous). X/IG/YouTube deferred or human-assisted. **Drop browser-automation fallback** in v1. Add `delete(external_url)` to the adapter Protocol. Add per-channel pacing (`scheduled_for` spacing + per-platform daily cap) and proactive OAuth refresh (on-fail → mark channel `failed` + alert).
- **Crank quality gate (merged):** single LLM critic call → `{score, safety_pass, notes}`; below threshold → regenerate (max N); `safety_pass=false` → hard block. **Plus** a non-LLM blocklist/regex guard and a claim-traces-to-brief check. **Plus** human spot-check: hold first item per channel + random 10%. Generator and critic use different model tiers; novelty enforced by feeding recent published items into the generator.
- **Landing site:** one `site-template/` as the whole site; AI fills copy slots + brand tokens (no bespoke layout gen). **Pre-QA smoke test** (build + 4 funnel events fire + Checkout hits test price) runs before the human QA gate.
- **Attribution:** UTM per published link → first-touch cookie → `lead.first_touch_token` → Stripe Checkout `client_reference_id`/metadata → webhook join → `metric_event(stage=paid, channel_id)`.
- **Observability:** daily heartbeat digest (published/failed/reach per channel) + alerts on repeated publish-fail, dead token, or zero-reach. Replaces Flower for the operator.
- **Cost:** per-product monthly token budget with hard stop; log token usage per `job_run`. Phase B media GPU is out-of-scope spend, decided separately.

---

## 1. Architecture overview

```
                         ┌──────────────────────────────┐
   Operator (Greg) ──────▶  Next.js dashboard (Nova)     │  VPS-firewalled, no auth
                         │  - product onboarding         │
                         │  - strategy review/edit       │
                         │  - QA checklist               │
                         │  - content calendar + metrics │
                         └──────────────┬───────────────┘
                                        │ REST (JSON)
                         ┌──────────────▼───────────────┐
                         │   FastAPI app (the brain)     │
                         │   modules: registry, strategy,│
                         │   setup, qa, crank, metrics   │
                         └───┬───────────────┬───────────┘
                  Postgres   │               │  enqueue
              (state, calendar,              │
               metrics, secrets)             ▼
                         ┌──────────────────────────────┐
                         │  Celery workers + beat        │
                         │  - strategy/setup jobs        │
                         │  - crank: generate→critic→    │
                         │    guardrail→publish          │
                         │  Redis broker · Flower monitor│
                         └───┬───────────────┬───────────┘
                Claude Agent │               │  publish
                SDK + content│               ▼
                gen skills   │     ┌──────────────────────┐
                             │     │ Channel adapters      │
                             │     │ API-first, browser fb │
                             │     │ YouTube/Reddit/X/blog │
                             │     └──────────────────────┘
                             ▼
                   Generated assets (landing site, media, content)
```

**Processes on the VPS (v1, per §0):** `fastapi` (uvicorn/gunicorn — runs APScheduler + an
in-process worker loop), `next` (dashboard), `nginx`. SQLite file for state. Plus generated
**per-product landing sites** (static export served by nginx). *Phase B adds `celery worker`/
`beat`/`flower`/`redis`/`postgres` when long media jobs arrive — the diagram above shows the
Phase-B shape.*

**Why this split:** the FastAPI app owns state + API and enqueues work; Celery owns anything
slow or scheduled (strategy analysis, media generation, publishing). The dashboard never does
heavy work — it reads state and triggers jobs.

---

## 2. Repository layout (monorepo)

```
saas-marketing-engine/
├── backend/                  # FastAPI + Celery (uv project)
│   ├── app/
│   │   ├── main.py           # FastAPI app + routers
│   │   ├── config.py         # settings (pydantic-settings)
│   │   ├── db.py             # SQLAlchemy/SQLModel engine + session
│   │   ├── models/           # ORM models (§4)
│   │   ├── api/              # routers: products, strategy, setup, qa, crank, metrics
│   │   ├── modules/
│   │   │   ├── strategy/     # codebase ingest → brief/brand/pricing
│   │   │   ├── setup/        # site gen, stripe, email, accounts, analytics
│   │   │   ├── qa/           # checklist gen + tracking
│   │   │   ├── crank/        # generators, critic, guardrail, scheduler
│   │   │   └── metrics/      # funnel + revenue aggregation
│   │   ├── channels/         # publishing adapters (§7)
│   │   ├── ai/               # Claude Agent SDK wrappers, prompts
│   │   ├── secrets/          # vault (§9)
│   │   └── tasks.py          # Celery app + task definitions
│   ├── tests/                # pytest + pytest-bdd
│   └── pyproject.toml
├── dashboard/                # Next.js (Nova template)
│   ├── app/
│   ├── components/
│   └── lib/api.ts
├── site-template/            # base for AI-generated landing sites (§6)
├── infra/
│   ├── docker-compose.yml    # postgres, redis, flower for local dev
│   ├── deploy/               # VPS service files, nginx, env templates
│   └── celery/               # beat schedule config
├── PRD.md · USER_STORIES.md · TECH_SPEC.md · BRAINSTORM.md
└── tasks/todo.md
```

`backend` uses `uv`; `dashboard` uses npm. Pre-commit: ruff + black (py), lint + tsc (ts).

---

## 3. Product lifecycle (state machine)

```
draft ──(strategy run)──▶ strategy ──(owner approves)──▶ setup_ready
  ──(setup run)──▶ setup_done ──(checklist emitted)──▶ qa
  ──(all blocking items pass)──▶ live ⇄ paused
                                  └──(crank runs on schedule while live)
```

State transitions are explicit API actions; invalid transitions are rejected. `paused`
halts new publishes but keeps generation history.

---

## 4. Data model (Postgres, SQLModel/SQLAlchemy)

```
product
  id, name, slug, repo_url, repo_local_path, description,
  monetization_model (enum: cc_sub | trial | freemium),
  lifecycle_state, created_at, updated_at

strategy_brief        (1:1 product)
  id, product_id, icp_json, pain_points_json, positioning,
  channel_plan_json, content_pillars_json, cadence_json,
  approved (bool), approved_at, raw_ai_output

brand_kit             (1:1 product)
  id, product_id, brand_name, voice_descriptors_json,
  visual_seeds_json, tone

pricing_plan          (1:N product)
  id, product_id, name, amount_cents, interval, stripe_price_id, features_json

channel               (1:N product)
  id, product_id, type (enum: landing|x|reddit|youtube|instagram),
  enabled, account_ref, connect_state (enum: pending|connected|failed)

credential            (1:N product)   # encrypted at rest (§9)
  id, product_id, channel_id (nullable), key, ciphertext, created_at

qa_checklist_item     (1:N product)
  id, product_id, ord, instruction, blocking (bool),
  status (enum: pending|pass|fail), comment, updated_at

content_item          (1:N product)
  id, product_id, channel_id, content_type (enum: social|blog|video|podcast),
  status (enum: generated|critic_passed|critic_failed|guardrail_failed|scheduled|published|publish_failed),
  body_ref, media_ref, critic_score, critic_notes, scheduled_for,
  published_at, external_url, error, created_at

metric_event          (1:N product)
  id, product_id, channel_id (nullable), stage (enum: impression|visit|signup|paid),
  value, occurred_at, source

job_run               (audit of Celery jobs)
  id, product_id, kind, status, started_at, finished_at, error
```

Revenue is derived from Stripe (webhooks → `metric_event(stage=paid)`), not stored as truth.

---

## 5. Strategy module

**Input:** product repo (local clone preferred; clone `repo_url` if no local path) + description.
**Process (Celery job):**
1. Ingest signal: README, package manifests, `docs/`, route/endpoint names, UI copy. Cap token use — summarize per-file, then synthesize (avoid dumping the whole repo into context).
2. Claude Agent SDK call → **Marketing Brief** (ICP, pain points, positioning, channel plan, content pillars, cadence).
3. → **Brand Kit** (name, voice descriptors, visual seeds, tone).
4. → **Pricing recommendation** mapped to `monetization_model`.

**Output:** rows in `strategy_brief`, `brand_kit`, `pricing_plan`. State → `strategy`.
Owner edits/approves in dashboard → `approved=true`, state → `setup_ready`.

**Quality:** the brief is the single source of truth the crank references. Store `raw_ai_output`
for debugging. One integration test: run against Auto Author's repo, assert non-empty ICP +
≥3 content pillars + a pricing plan.

---

## 6. Setup module + the funnel contract

### 6.1 Landing-site generation (bespoke design, standard plumbing)
The engine generates a **bespoke** site per product but every site MUST implement the
**Funnel Contract** — a fixed set of embedded components with stable IDs/events:

| Contract component | Requirement |
|---|---|
| `<EmailCapture>` | POSTs to `/api/funnel/{product}/lead`; fires `funnel:signup` event |
| `<StripeCheckout>` | Uses the product's `stripe_price_id`; redirects to Stripe Checkout |
| `<AnalyticsSnippet>` | Emits `funnel:visit` on load; self-hosted, no third-party paid SaaS |
| Conversion events | `impression`, `visit`, `signup`, `paid` posted to the metrics API |

Implementation: `site-template/` holds the contract components + an `nginx` static-export
pipeline. AI generates **copy, layout, sections, and styling** around those fixed components
(so design is unique, plumbing is identical → QA stays cheap). Sites deploy to the VPS under
the product's marketing domain (Auto Author → `autoauthor.app`; keep separate from
`dev.autoauthor.app` staging).

### 6.2 Stripe
Create Stripe product + prices for the model (`cc_sub` / `trial` / `freemium`). Store
`stripe_price_id` on `pricing_plan`. Subscribe via Stripe Checkout (no custom card form).
Stripe webhook → `metric_event(stage=paid)` + subscription lifecycle. **Test mode** until QA passes.

### 6.3 Email
Lightweight: store leads in `product`-scoped table; welcome email via SMTP (existing infra) or
a free-tier ESP. No paid ESP in v1.

### 6.4 Accounts + human checklist
For each enabled `channel`, the engine prepares what it can (handles, bios, profile copy from
brand kit) and emits **only the human-required steps** (CAPTCHA account creation, OAuth consent,
ToS acceptance, DNS, Stripe/banking) as ordered checklist items. OAuth tokens land in the vault
via per-platform connect flows in the dashboard.

### 6.5 Analytics
Self-hosted funnel events table fed by the contract. No new paid service (per NFR-3).

**Output:** deployed site, Stripe config, email capture live, channel rows in `pending`/`connected`,
launch checklist generated. State → `setup_done` → `qa`.

---

## 7. Channel adapters (publishing)

Uniform interface; API-first, browser fallback:

```python
class ChannelAdapter(Protocol):
    type: ChannelType
    def supports_api(self) -> bool: ...
    def publish(self, item: ContentItem, creds: Credentials) -> PublishResult: ...
    # PublishResult: external_url | error; raises Retryable for transient failures
```

| Channel | v1 approach |
|---|---|
| Blog (landing site) | Direct write to the product's site repo/CMS (API/file) — fully owned, lowest risk |
| YouTube | YouTube Data API (OAuth) |
| Reddit | Reddit API (PRAW, OAuth) |
| X | X API where the account tier allows; else browser fallback |
| Instagram | Graph API where feasible; else browser fallback / human-assisted |

Browser fallback uses the existing web-ctl/Playwright tooling, isolated per channel so one
flaky platform can't block others. Every publish writes `content_item.status` + `external_url`
or `error`; transient failures retry via Celery.

> Risk flag (PRD §12): X/IG autonomous posting on owned accounts may be ToS-constrained →
> some channels may degrade to human-assisted posting. The kill switch (NFR-6) is per channel.

---

## 8. Crank module

### 8.1 Scheduling
Celery **beat** schedule per product (default weekly batch; `cadence_json` overrides). A beat
tick enqueues a `crank(product_id)` job that fans out per enabled channel × content type.

### 8.2 Pipeline per content item
```
generate ──▶ critic (separate AI pass, score 0–1) ──▶ brand-safety guardrail ──▶ schedule ──▶ publish ──▶ record metrics
              │ < threshold                            │ fail
              ▼                                         ▼
        regenerate (max N) or skip+log           block + log (never publishes)
```
- **Generators** (per content type): `social`, `blog` (Phase A); `video`, `podcast` (Phase B).
  Reuse existing skills/tools: video-podcast-maker, ElevenLabs/ACE-Step, manim, podcast-studio-hub
  generator→critic pattern. Each generator consumes the brief + brand kit.
- **Critic**: independent Claude call scoring against pillars/brand; `critic_score` + `critic_notes`.
  Threshold configurable (default 0.7). Below → regenerate up to N (default 2), else skip + log.
- **Guardrail**: brand-voice + safety/compliance check; hard block on fail (own reputation at stake).
- **Publish**: via §7 adapters. **Pause** (`paused` or per-channel kill switch) is checked
  immediately before publish.

### 8.3 Idempotency & resilience
Long media jobs are separate Celery tasks with retries; a crashed video job never blocks
social/blog for the same or other products. `job_run` records every task for the operator.

---

## 9. Secrets / credentials vault
- Symmetric encryption (Fernet/`cryptography`) with a key from env (`SME_VAULT_KEY`), not in DB.
- `credential.ciphertext` only; plaintext never logged (lint rule + redaction in logger).
- OAuth refresh tokens stored same way; refreshed on demand by adapters.
- ponytail: single global vault key for v1 (single owner). Per-product keys only if isolation
  ever matters → not now.

---

## 10. AI integration
- **Claude Agent SDK** for strategy, generation, critic, guardrail, checklist generation.
  Latest Claude models (`claude-opus-4-8` for synthesis/strategy; cheaper tier for bulk
  generation/critic where quality allows).
- Prompts versioned in `app/ai/prompts/`. Each AI call logs token usage to `job_run` for cost visibility.
- Content-gen tools (audio/video/animation) invoked as subprocess/skill calls from generators.

---

## 11. Deployment (Hostinger dev VPS, 195.35.14.177)
- **Check port conflicts before binding** (existing services on the box — see server memory).
  v1 ports (verify free): FastAPI `:8010`, dashboard `:3010`. SQLite is a file (no port).
  Phase B adds Postgres/Redis/Flower ports then.
- **Public vs private (per §0):** dashboard + private API on the firewalled/private interface
  (no auth); the **public funnel-ingest API** (`/api/funnel/*`, `/api/stripe/webhook`) and the
  generated landing sites are internet-facing via nginx, rate-limited, with CORS for the product origin.
- nginx reverse-proxies the dashboard (private/allowlisted) and serves generated landing sites
  (public, per product domain).
- **CORS**: dashboard origin ↔ FastAPI must be configured before first remote deploy.
- CI/CD idempotent; feature branch → PR → main; pre-commit hooks required.
- Dashboard firewalled (no auth) — bind private interface; reach via SSH tunnel or IP allowlist
  (preserve the Cox /17 whitelist noted in server memory).

---

## 12. Testing
- TDD, >85% coverage, 100% pass. Integration/E2E use **real services** (real Postgres/Redis;
  Stripe test mode; a throwaway test channel where possible) — no mocking per house standard.
- Key integration tests: strategy run on Auto Author repo; funnel-contract events fire on a
  generated site; crank produces → critic-gates → records a `content_item`; Stripe webhook →
  `paid` metric; per-channel kill switch halts publish.
- `conducting-demo` skill for the acceptance demo (DoD-3, Auto Author end-to-end).

---

## 13. Phase → issue mapping (dependency-ordered)

**P0 Foundation** → S0.1 scaffold · S0.2 infra (compose + VPS, port check) · S0.4 product model+API+onboarding form · S0.5 vault · (S0.3 skipped: no auth in v1).
**P1 Strategy** → S1.1 ingest+brief · S1.2 brand kit · S1.3 pricing rec · S1.4 review/approve UI.
**P2 Setup** → site-template + funnel contract (S2.1) · Stripe configurable (S2.2) · email (S2.3) · accounts+checklist+OAuth connect (S2.4) · analytics wiring (S2.5) · launch checklist (S2.6).
**P3 QA gate** → checklist gen (S3.1) · pass/fail + go-live block (S3.2).
**P4 Crank core** → beat scheduler (S4.1) · social+blog generators (S4.2) · critic gate (S4.3) · guardrail (S4.4) · publish adapters API-first+fallback (S4.5) · kill switch (S4.6).
**P5 Crank media** → video pipeline (S5.1) · podcast pipeline (S5.2).
**P6 Metrics & acceptance** → funnel/revenue dashboard (S6.1) · calendar+job health (S6.2) · Auto Author end-to-end (S6.3, DoD).

Each bullet = one or a few atomic issues; within a phase, model/API/migration land before UI;
adapters land before the crank that calls them.

---

## 14. Deferred (explicitly not v1)
B2E pipeline · paid ads (stage 4) · multi-tenant/customer auth · per-product vault keys ·
operator login (until dashboard is publicly exposed) · paid ESP/analytics SaaS.
