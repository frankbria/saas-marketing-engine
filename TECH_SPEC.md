# Technical Specification — SaaS Marketing Engine (SME)

*Version 0.2 · 2026-06-28 · Derives from PRD.md + USER_STORIES.md*
*Changelog v0.2: v1 storage/scheduler = SQLite + APScheduler (Celery/Postgres → Phase B); public/private API split; templated site; merged+hardened quality gate; attribution chain; idempotency/novelty/pacing/retract; failure detection; data-model trims; honest cost model.*

This spec is the implementation contract. Each section maps to dependency-ordered GitHub issues
(see §13). Scope is **v1, single-owner**, with **Auto Author as the first fixture** (not a special case).

> **Design rule (PRD G7): zero product-specific hardcoding.** No Auto-Author-specific branch
> may exist in engine code. Every product-specific value (repo, domain, brand, ICP, channels,
> pricing, credentials, cadence) lives in the **Product record / config**. Auto Author appears
> only as test-fixture data and the acceptance demo. Any new product must run end-to-end with
> zero code changes. Reviewers reject PRs that reference a specific product by name in logic.

---

## 1. Architecture overview (v1)

```
                         ┌──────────────────────────────┐
   Operator (Greg) ──────▶  Next.js dashboard (Nova)     │  private interface, no auth
                         │  onboarding · strategy review │
                         │  QA checklist · metrics       │
                         └──────────────┬───────────────┘
                                        │ REST (private API)
   public visitors ───────┐            │
   (landing sites) ───────┼────────────┤
                          ▼            ▼
         ┌────────────────────────────────────────────┐
         │            FastAPI app (the brain)          │
         │  PRIVATE router: registry/strategy/setup/   │
         │    qa/crank/metrics (firewalled)            │
         │  PUBLIC router: /api/funnel/* (visit/lead), │
         │    /api/stripe/webhook (rate-limited, CORS) │
         │  APScheduler (in-process) → crank jobs       │
         └───┬──────────────────┬──────────────────────┘
       SQLite│ (state, calendar, │ in-process worker loop
       (WAL) │  metrics, leads,  │ (job_run rows, retries)
             │  encrypted creds) ▼
   Claude    │        ┌──────────────────────────────┐
   Agent SDK │        │ crank: generate → critic+safety│
   (gen+critic,       │  → deterministic guard →       │
    diff tiers)       │  pace/schedule → publish        │
             │        └───────────┬──────────────────┘
             ▼                    │ publish (API-first)
   per-product workspace          ▼
   (templated site, content) ┌──────────────────────┐
                             │ Channel adapters       │
                             │ blog (owned) · Reddit  │  v1
                             │ · YouTube (video, S5.1)│
                             │ [IG/X deferred]        │
                             └────────────────────────┘
```

**Processes on the VPS (v1):** `fastapi` (uvicorn/gunicorn — hosts both routers + APScheduler +
the in-process worker loop), `next` (dashboard), `nginx` (fronts public landing sites + the
public funnel API; reverse-proxies the private dashboard on the allowlisted interface). SQLite
file for state. Generated **per-product landing sites** are static-exported and served by nginx.

**Phase B** introduces `celery worker` / `beat` / `flower` / `redis` / `postgres` and an **ephemeral
rented GPU worker** — provisioned from a commercial provider when `media`-queue jobs are pending,
torn down when idle (decided 2026-07-03) — **only when** long media (video/podcast) jobs make a
real queue + parallel workers load-bearing. Control plane stays on the VPS; only GPU minutes are rented.

**Why this split:** the FastAPI app owns state + both API surfaces and schedules work via
APScheduler; an in-process worker loop runs crank jobs with `job_run`-tracked retries. The
dashboard never does heavy work — it reads state and triggers jobs. The public API surface is
the *only* internet-facing entry to the brain, and it's narrow (visit/lead/webhook).

---

## 2. Repository layout (monorepo)

```
saas-marketing-engine/
├── backend/                  # FastAPI (uv project)
│   ├── app/
│   │   ├── main.py           # FastAPI app: private + public routers
│   │   ├── config.py         # settings (pydantic-settings)
│   │   ├── db.py             # SQLModel engine + session (SQLite WAL)
│   │   ├── models/           # ORM models (§4)
│   │   ├── api/
│   │   │   ├── private/      # products, strategy, setup, qa, crank, metrics
│   │   │   └── public/       # funnel (visit/lead), stripe webhook
│   │   ├── modules/
│   │   │   ├── strategy/     # codebase ingest → brief/brand/pricing
│   │   │   ├── setup/        # site gen, stripe, email, accounts, attribution
│   │   │   ├── qa/           # checklist gen + tracking
│   │   │   ├── crank/        # generators, critic, guard, scheduler, worker loop
│   │   │   └── metrics/      # funnel + attribution + heartbeat
│   │   ├── channels/         # publishing adapters (§7)
│   │   ├── ai/               # Claude Agent SDK wrappers, prompts, budget
│   │   ├── secrets/          # vault (§9)
│   │   └── scheduler.py      # APScheduler setup + job_run worker loop
│   ├── tests/                # pytest + pytest-bdd
│   └── pyproject.toml
├── dashboard/                # Next.js (Nova template)
│   ├── app/ · components/ · lib/api.ts
├── site-template/            # the landing-site template (AI fills copy + brand tokens) (§6)
├── infra/
│   ├── deploy/               # VPS service files, nginx, env templates
│   └── compose.dev.yml       # (Phase B) postgres/redis/flower for local dev
├── PRD.md · USER_STORIES.md · TECH_SPEC.md · BRAINSTORM.md
└── tasks/todo.md
```

`backend` uses `uv`; `dashboard` uses npm. Pre-commit: ruff + black (py), lint + tsc (ts).

---

## 3. Product lifecycle (state machine)

```
draft ──(strategy run)──▶ strategy ──(owner approves)──▶ setup_ready
  ──(setup run)──▶ setup_done ──(smoke-test pass + checklist emitted)──▶ qa
  ──(all blocking items pass)──▶ live ⇄ paused
                                  └──(crank runs on schedule while live)
```

State transitions are explicit API actions; invalid transitions are rejected. `paused`
halts new publishes but keeps generation history. A failed pre-QA smoke test keeps the product
in `setup_done` (never reaches `qa`).

---

## 4. Data model (SQLite/WAL via SQLModel; Postgres-ready)

```
product
  id, name, slug, repo_url, repo_local_path, description,
  monetization_model (enum: cc_sub | trial | freemium),   # v1 implements cc_sub
  brand_json,                                             # folded brand kit
  price_amount_cents, price_interval, stripe_price_id,    # folded pricing (cc_sub)
  marketing_domain,
  token_budget_cents_month,                               # per-product hard cap
  lifecycle_state, created_at, updated_at

strategy_brief        (1:1 product)
  id, product_id, icp_json, pain_points_json, positioning,
  channel_plan_json, content_pillars_json, cadence_json,
  approved (bool), approved_at, raw_ai_output

channel               (1:N product)
  id, product_id, type (enum: blog|reddit|x|instagram|youtube),
  enabled, autonomous (bool),                    # v1: blog/reddit/youtube true (S5.1); x/instagram false
  account_ref, connect_state (enum: pending|connected|failed),
  daily_cap, paused (bool)

credential            (1:N product)   # encrypted at rest (§9)
  id, product_id, channel_id (nullable), key, ciphertext,
  expires_at, created_at

lead                  (1:N product)
  id, product_id, email, first_touch_token, created_at

qa_checklist_item     (1:N product)
  id, product_id, ord, instruction, blocking (bool),
  status (enum: pending|pass|fail), comment, updated_at

content_item          (1:N product)
  id, product_id, channel_id, content_type (enum: social|blog|video|podcast),
  status (enum: generated|critic_passed|critic_failed|guard_failed|
                scheduled|published|publish_failed|retracted|
                rendering|render_failed),                        # rendering/render_failed: S5.1
  body_ref, media_ref, critic_score, critic_notes,
  tracking_token,                # UTM threaded into published links
  idempotency_key,               # unique per (content_item, channel)
  spot_check (bool),             # flagged for async human review
  scheduled_for, published_at, external_url, error, created_at

metric_event          (1:N product)
  id, product_id, channel_id (nullable), content_item_id (nullable),
  stage (enum: impression|visit|signup|paid), value, occurred_at, source

job_run               (audit of scheduled/worker jobs)
  id, product_id, kind, status, attempts, token_cost_cents,
  started_at, finished_at, error
```

Revenue is derived from Stripe webhooks → `metric_event(stage=paid)`, attributed via the chain
in §6.6. `brand_kit`/`pricing_plan` are intentionally **not** separate tables in v1 (1:1 / single-plan);
promote to tables only if multi-plan pricing or richer brand modeling materializes.

---

## 5. Strategy module

**Input:** product repo (local clone preferred; clone `repo_url` if no local path) + description.
**Process (scheduled job):**
1. Ingest signal: README, package manifests, `docs/`, route/endpoint names, UI copy. Cap token use — summarize per-file, then synthesize (never dump the whole repo into context). Token cost recorded to `job_run`, checked against `product.token_budget_cents_month`.
2. Claude Agent SDK call → **Marketing Brief** (ICP, pain points, positioning, channel plan, content pillars, cadence).
3. → **Brand Kit** → `product.brand_json` (name, voice descriptors, visual seeds, tone).
4. → **Pricing recommendation** for `cc_sub` → `product.price_*`.

**Output:** `strategy_brief` row + brand/pricing fields on `product`. State → `strategy`.
Owner edits/approves in dashboard → `approved=true`, state → `setup_ready`.

**Quality:** the brief is the single source of truth the crank references (incl. the claim-trace
guard, §8). Store `raw_ai_output` for debugging. Integration test: run against Auto Author's repo,
assert non-empty ICP + ≥3 content pillars + a populated price.

---

## 6. Setup module + the funnel contract

### 6.1 Landing-site generation (templated design, standard plumbing)
The engine builds each site from **one maintained `site-template/`**, injecting AI-written
**copy slots + brand tokens** (palette/font/voice from `brand_json`). Layout/structure/plumbing
are constant → QA is a fixed, cheap checklist. Every site implements the **Funnel Contract**:

| Contract component | Requirement |
|---|---|
| `<EmailCapture>` | POSTs to the **public** `/api/funnel/{slug}/lead`; fires `signup` event |
| `<StripeCheckout>` | Uses `product.stripe_price_id`; redirects to Stripe Checkout with `client_reference_id = first_touch_token` |
| `<AnalyticsSnippet>` | Emits `visit` on load (+ `impression` where available); self-hosted |
| UTM capture | Reads UTM params on landing, sets a first-touch cookie (`first_touch_token`) |

Sites are static-exported and served by nginx under `product.marketing_domain` (Auto Author →
`autoauthor.app`; kept separate from `dev.autoauthor.app` staging).

### 6.2 Public funnel-ingest API (the split)
A **public**, internet-facing router separate from the private dashboard API:
`POST /api/funnel/{slug}/visit`, `POST /api/funnel/{slug}/lead`, `POST /api/stripe/webhook`.
Rate-limited, strictly validated, CORS for the product origin only. The private dashboard/operator
API stays on the firewalled interface (NFR-1).

### 6.3 Stripe (cc_sub)
Create Stripe product + price for `cc_sub`; store `stripe_price_id` on `product`. Subscribe via
Stripe Checkout (no custom card form), passing `client_reference_id`/metadata for attribution.
Webhook → `metric_event(stage=paid)` + subscription lifecycle. **Test mode** until QA passes.
(The `trial`/`freemium` enum values are unwired in v1.)

### 6.4 Email
Store leads in the `lead` table; send **one welcome email** via SMTP (existing infra) or free-tier
ESP. Drip deferred. Setup checklist includes **SPF/DKIM/DMARC** for the product domain (else welcome
mail spam-folders).

### 6.5 Accounts + human checklist
For each enabled `channel`, the engine prepares what it can (handles, bios, profile copy from
`brand_json`) and emits **only human-required steps** (CAPTCHA account creation, OAuth consent, ToS
acceptance, DNS + email auth, Stripe/banking) as ordered checklist items. OAuth tokens land in the
vault via per-platform connect flows in the dashboard. New accounts get a **warm-up** note before
any links are posted (cold-account ban mitigation).

### 6.6 Analytics + attribution chain
Self-hosted funnel events fed by the contract. Attribution: **UTM per published link → first-touch
cookie (`first_touch_token`) → `lead.first_touch_token` → Stripe Checkout `client_reference_id` →
webhook join → `metric_event(stage=paid, channel_id, content_item_id)`.** Without this chain,
revenue can't be attributed to a channel/content item.

### 6.7 Pre-QA smoke test
Before a product can reach `qa`, an automated test asserts: the generated site **builds**, the four
funnel events (`impression/visit/signup/paid` path) **fire**, and Checkout hits the correct **test
price**. Failure keeps the product in `setup_done`. The human QA gate then covers product + design/content only.

**Output:** deployed site, public funnel API live, Stripe config (test mode), email capture live,
channel rows, attribution wired, smoke test passed, launch checklist generated. State → `setup_done` → `qa`.

---

## 7. Channel adapters (publishing)

Uniform interface; **API-first only in v1** (no browser fallback):

```python
class ChannelAdapter(Protocol):
    type: ChannelType
    def publish(self, item: ContentItem, creds: Credentials) -> PublishResult: ...
    def delete(self, external_url: str, creds: Credentials) -> None: ...   # retract
    # PublishResult: external_url | error; raises Retryable for transient failures
    # publish MUST be idempotent on item.idempotency_key (check remote before re-posting)
```

| Channel | v1 | approach |
|---|---|---|
| Blog (owned site) | ✅ autonomous | Direct write to the product's site repo/CMS (API/file) — fully owned, zero ToS risk |
| Reddit | ✅ autonomous (cautious) | PRAW (OAuth); warmed account, value-first/non-promo content policy, per-subreddit rules |
| YouTube | ✅ autonomous (S5.1) | video pipeline (script→TTS→GPU render) + YouTube Data API v3 resumable upload |
| X | ⏸ deferred/human-assisted | API tier gated; cold-account risk |
| Instagram | ⏸ deferred/human-assisted | Graph API painful + ToS-hostile to autonomous posting |

Every publish writes `content_item.status` + `external_url` or `error`; transient failures retry
via the `job_run` worker loop. **Pacing:** `scheduled_for` is spread across the cadence window with
a per-channel `daily_cap`. **OAuth refresh** is proactive; on refresh failure the adapter marks the
`channel` `failed`, halts its publishes, and fires an alert (§8.4). Per-channel kill switch
(`channel.paused`) is checked immediately before every publish.

---

## 8. Crank module

### 8.1 Scheduling
**APScheduler** runs a per-product job on the product's cadence (default weekly; `cadence_json`
overrides). A tick inserts a `crank(product_id)` `job_run` that fans out per enabled **autonomous**
channel × content type. The in-process worker loop executes jobs with retries (`job_run.attempts`).

### 8.2 Pipeline per content item
```
generate ─▶ critic+safety (1 LLM call) ─▶ deterministic guard ─▶ pace/schedule ─▶ publish ─▶ record metrics
   │             │ score<thresh  │ safety_pass=false   │ blocklist/claim-trace fail
   │ (novelty:   ▼               ▼                      ▼
   │  recent    regenerate    hard block + log      hard block + log
   │  items fed  (max N)       (never publishes)     (never publishes)
   │  in)
```
- **Generators** (per content type): `social`, `blog` (Phase A); `video`, `podcast` (Phase B).
  Each consumes the brief + `brand_json`. **Novelty:** recent published items for the channel are
  fed into the prompt to avoid near-duplicates.
- **Critic + safety (one call):** independent Claude call (different model tier than the generator)
  returns `{score, safety_pass, notes}`. `score < threshold` (default 0.7) → regenerate up to N
  (default 2) else skip+log; `safety_pass=false` → hard block (`guard_failed`).
- **Deterministic guard (non-LLM):** a blocklist/regex check + a **claim-trace** check that every
  factual claim in the item maps to the strategy brief/product facts. Hard block on failure.
- **Publish:** via §7 adapters, idempotent on `idempotency_key`. **Pause / kill switch** checked
  immediately before publish.
- **Spot-check:** the first item per channel + a random 10% are flagged `spot_check=true` for
  **async** human review in the dashboard — never blocks publishing.

### 8.3 Idempotency & resilience
At-least-once retries make publish idempotent (check remote by `idempotency_key` before posting).
A crashed job never blocks other products/channels. `job_run` records every task + token cost.
(Phase B: long media jobs move to Celery workers for true parallelism.)

### 8.4 Observability / failure detection
A daily **heartbeat** job emits a digest per product (published / failed / reach per channel) and
fires **alerts** on: repeated publish-fail, dead/expired OAuth token, or **zero-reach** over a
window (shadowban signal). This is what makes "unattended ≥2 weeks" verifiable; it replaces Flower
for the operator.

---

## 9. Secrets / credentials vault
- Symmetric encryption (Fernet/`cryptography`) with a key from env (`SME_VAULT_KEY`), not in DB.
- `credential.ciphertext` only; plaintext never logged (lint rule + redaction in logger).
- OAuth refresh tokens stored the same way; **proactively refreshed**; on failure → channel `failed` + alert (§8.4).
- ponytail: single global vault key for v1 (single owner). Per-product keys only if isolation ever matters → not now.

---

## 10. AI integration
- **Claude Agent SDK** for strategy, generation, critic+safety, deterministic-guard orchestration, checklist generation. Latest Claude models — **`claude-opus-4-8` for strategy synthesis; a cheaper tier for bulk generation and the critic** (deliberately different tier than the generator).
- **Budget:** every AI call logs token cost to `job_run`; a per-product **monthly budget hard-stop** (`product.token_budget_cents_month`) blocks further generation when exceeded (logged + surfaced).
- Prompts versioned in `app/ai/prompts/`. Phase B content-gen tools (audio/video/animation) invoked as subprocess/skill calls from generators.

---

## 11. Deployment (Hostinger dev VPS, 195.35.14.177)
- **Check port conflicts before binding** (existing services — see server memory). v1 ports (verify free): FastAPI `:8010`, dashboard `:3010`. SQLite is a file (no port). Phase B adds Postgres/Redis/Flower ports.
- **Public vs private:** dashboard + private API on the firewalled/private interface (no auth, SSH tunnel / IP allowlist — preserve the Cox /17 whitelist in server memory). The **public funnel API** (`/api/funnel/*`, `/api/stripe/webhook`) + generated landing sites are internet-facing via nginx, rate-limited, CORS for the product origin.
- **CORS** configured before first remote deploy. CI/CD idempotent; feature branch → PR → main; pre-commit hooks required.

---

## 12. Testing
- TDD, >85% coverage, 100% pass. Integration/E2E use **real services** (real SQLite; Stripe test mode; a throwaway test channel/subreddit where possible) — no mocking per house standard.
- Key integration tests: strategy run on Auto Author repo; funnel-contract events fire on a generated site + **pre-QA smoke test**; **attribution** (UTM → lead → Stripe `client_reference_id` → webhook → attributed `paid` metric); crank produces → critic+guard gate → idempotent publish → records `content_item`; per-channel kill switch halts publish; retract deletes a published item; heartbeat fires a zero-reach alert.
- `conducting-demo` skill for the acceptance demo (DoD-3, Auto Author end-to-end).

---

## 13. Phase → issue mapping (dependency-ordered; story IDs per USER_STORIES.md)

**P0 Foundation** → S0.1 scaffold · S0.2 SQLite + APScheduler + job_run + infra/port-check · S0.3 product model+API+onboarding form · S0.4 vault.
**P1 Strategy** → S1.1 ingest+brief · S1.2 brand kit (JSON) · S1.3 cc_sub pricing rec · S1.4 review/approve UI.
**P2 Setup** → S2.1 templated site + funnel contract + UTM · S2.2 public funnel-ingest API split · S2.3 Stripe cc_sub · S2.4 email capture + welcome · S2.5 attribution chain · S2.6 accounts + human checklist (SPF/DKIM) + OAuth connect · S2.7 pre-QA smoke test · S2.8 launch checklist.
**P3 QA gate** → S3.1 checklist gen · S3.2 pass/fail + go-live block.
**P4 Crank core** → S4.1 APScheduler crank + worker loop · S4.2 social+blog generators + novelty · S4.3 critic+safety (1 call) · S4.4 deterministic guard (blocklist + claim-trace) · S4.5 publish adapters (blog + Reddit, idempotency + pacing) · S4.6 per-channel kill switch · S4.7 retract (delete) · S4.8 OAuth refresh handling · S4.9 spot-check sampling.
**P5 Crank media** → S5.0 Celery/Redis/Postgres + ephemeral rented GPU infra · S5.1 video pipeline · S5.2 podcast pipeline.
**P6 Metrics & acceptance** → S6.1 attributed funnel+revenue dashboard · S6.2 heartbeat + alerts · S6.3 content calendar · S6.4 Auto Author end-to-end (DoD).

Each bullet = one or a few atomic issues; within a phase, model/migration/API land before UI;
adapters land before the crank that calls them; the public-API split (S2.2) lands before the site (S2.1) goes live.

---

## 14. Deferred (explicitly not v1)
B2E pipeline · paid ads · multi-tenant/customer auth · per-product vault keys · operator login
(until dashboard is publicly exposed) · paid ESP/analytics SaaS · browser-automation publishing ·
X/IG autonomous posting · podcast (Phase B, S5.2) · `trial`/`freemium` monetization ·
Celery/Redis/Postgres (until Phase B) · portfolio roll-up dashboard.
