# SME Backend

FastAPI backend for the SaaS Marketing Engine. Two API surfaces (TECH_SPEC §1):

- **private** (`/api/private/*`) — dashboard/operator API, firewalled, no auth in v1
- **public** (`/api/public/*`) — funnel-ingest (visit/lead/checkout) + Stripe webhook, internet-facing

## Develop

```bash
uv sync                       # install (Python 3.13, pinned in .python-version)
uv run uvicorn app.main:app --reload --port 8010
uv run pytest                 # tests
uv run ruff check . && uv run black --check .   # lint + format
```

Module skeleton under `app/` (`modules/{strategy,setup,qa,crank,metrics}`, `channels/`, `ai/`,
`secrets/`) is populated by later phase issues.

### Storage + jobs (S0.2)

- `db.py` — SQLModel engine on SQLite (WAL + `busy_timeout`); `init_db()` bootstraps the schema
  (no Alembic in v1). Postgres swap-in is a Phase B change to `SME_DATABASE_URL`.
- `models/job_run.py` — `JobRun` audit row with an `attempts` retry column.
- `worker.py` — in-process worker loop: `enqueue()` + `run_due_jobs()` (retries failures up to
  `MAX_ATTEMPTS`) + `reclaim_running_jobs()` (recovers jobs orphaned by a crash on startup).
- `scheduler.py` — APScheduler `BackgroundScheduler` (heartbeat enqueues a noop, worker tick
  drains the queue), started from the app lifespan.

### Product registry (S0.3)

- `models/product.py` — `Product`, the first-class unit (TECH_SPEC §4). All product-specific
  config (repo, domain, pricing, brand, budget) lives on this row (PRD G7). Defaults:
  `monetization_model=cc_sub`, `lifecycle_state=draft`.
- `api/private/products.py` — CRUD at `/api/private/products` (POST/GET/PATCH/DELETE).
  `lifecycle_state` is not raw-editable here; transitions go through the state machine in
  later phases (S1.4 / S3.2).
- `workspace.py` — on create, scaffolds `{SME_WORKSPACE_ROOT}/{slug}/vault/` (the empty
  credentials vault; Fernet encryption lands in S0.4). `SME_WORKSPACE_ROOT` defaults to
  `./workspace`.
- `SME_CORS_ORIGINS` (comma-separated; default `http://localhost:3010`) — browser origins
  allowed to call the private API.

### Credentials vault (S0.4)

- `models/credential.py` — `Credential` (TECH_SPEC §4): only Fernet `ciphertext` at rest,
  scoped by `(product_id, key, channel_id)`; `__repr__` omits the ciphertext.
- `secrets/vault.py` — `encrypt`/`decrypt` + `put_credential`/`get_credential`. Symmetric
  Fernet key from env **`SME_VAULT_KEY`** (a `SecretStr`, never stored in the DB). Generate one
  (run from `backend/` so the `app` package resolves):
  `cd backend && uv run python -c "from app.secrets.vault import generate_key; print(generate_key())"`,
  then put it in `backend/.env` (copy `backend/.env.example` to start).
  `install_redaction()` (wired into the app lifespan) installs a global log-record factory
  that scrubs every vault secret from all logs; `tests/test_no_plaintext_logging.py` is the
  static backstop. Single global key for v1 (per-product keys deferred — §9).

### Strategy brief (S1.1)

- `models/strategy_brief.py` — `StrategyBrief` (1:1 product, TECH_SPEC §4/§5): ICP, pain
  points, positioning, channel plan, content pillars, cadence, `raw_ai_output`.
- `modules/strategy/ingest.py` — bounded repo ingest (README/manifests/docs + route & UI/copy
  source; dot-dirs and symlink escapes excluded). Local clone preferred; `repo_url` is
  re-cloned fresh each run.
- `ai/client.py` + `ai/pricing.py` — Anthropic SDK: Haiku per-file summaries, Opus 4.8
  synthesis via structured outputs; per-call cost in cents. Needs env **`SME_ANTHROPIC_API_KEY`**
  (a `SecretStr`).
- `modules/strategy/brief.py` — `@handler("strategy_brief")`: budget-gated (pre-check +
  mid-loop cap + synthesis reservation vs `token_budget_cents_month`, `0`=unlimited) ingest →
  summarize → synthesize → upsert brief → product → `strategy`. Cost is recorded to `job_run`.
- `modules/strategy/brand.py` — `@handler("brand_kit")`: budget-gated Opus call grounded in the
  product's brief → folds a Brand Kit (name, tone, structured voice descriptors, visual seeds)
  onto `product.brand_json`. No new table, no lifecycle change. Cost recorded to `job_run`.
- `modules/strategy/pricing.py` — `@handler("pricing")` (S1.3): budget-gated Opus call grounded
  in the brief → folds a `cc_sub` price (`price_amount_cents` + `price_interval`, month/year) onto
  the product. Refused for non-`cc_sub` products (trial/freemium unwired in v1). Owner-editable via
  the products PATCH. No new table, no lifecycle change. Cost recorded to `job_run`.
- `api/private/strategy.py` — `POST /api/private/strategy/{product_id}/brief`, `.../brand`, and
  `.../pricing` enqueue their jobs (202; `brand`/`pricing` 400 until a brief exists, `pricing` also
  400s for non-`cc_sub`). The real-API integration tests are gated on `SME_ANTHROPIC_API_KEY`.
- `modules/setup/site.py` — `@handler("setup_site")` (S2.1): budget-gated Opus call writes on-brand
  landing copy + design tokens from `product.brand_json`, renders the one maintained
  `site-template/index.html.j2` (Jinja2, autoescaped), statically exports it to
  `{workspace}/{slug}/site/`, and deploys it under `marketing_domain` (`SME_NGINX_SITES_ROOT/{domain}/`
  + an emitted vhost). The generated site's funnel JS calls the public API at
  `SME_PUBLIC_API_BASE_URL` — UTM→first-touch cookie, `visit` on load, `lead` on submit, checkout
  carrying `client_reference_id`. `marketing_domain` is hostname-validated before any filesystem use.
- `api/private/setup.py` — `POST /api/private/setup/{product_id}/site` enqueues the site build (202;
  404 missing, 409 unless `setup_ready`, 400 until a brand kit exists).
- `integrations/stripe_api.py` (S2.3) — stdlib-only Stripe REST calls (Product, recurring Price,
  Checkout Session); no `stripe` SDK, no new runtime dep. Needs env **`SME_STRIPE_API_KEY`**
  (`sk_test_…` in dev); non-2xx raises loudly. ponytail: no Idempotency-Key/retry until live mode.
- `modules/setup/stripe_setup.py` — `@handler("stripe_setup")` (S2.3): creates the Stripe
  product+price and folds `stripe_price_id` onto the product. `cc_sub` only, idempotent, interval
  must be `month`/`year`. Triggered by `POST /api/private/setup/{product_id}/stripe` (202; 404
  missing, 409 unless `setup_ready`, 400 for non-`cc_sub` / no price / bad interval).
- `api/public/funnel.py` — `POST /api/funnel/{slug}/checkout` (S2.3) starts a subscription Checkout
  session passing `client_reference_id` + `metadata[first_touch_token]` for the S2.5 attribution
  join; requires an attribution token (422 without), 409 until Stripe is configured, returns `{url}`.
  The products PATCH keeps the invariant *`stripe_price_id` set ⟹ priced `cc_sub`*: a real price
  change or a switch off `cc_sub` clears it (no-op resubmits preserved). Real-API tests gated on
  `SME_STRIPE_API_KEY`.
- `models/metric_event.py` (S2.5) — `MetricEvent` (TECH_SPEC §4): `product_id`, nullable
  `channel_id`/`content_item_id` (`content_item` arrived in S4.2), `stage`
  (`impression|visit|signup|paid`), `value` (cents for `paid`), `occurred_at`, and a `source`
  provenance/idempotency key (`unique`).
- `api/public/stripe.py` (S2.5) — closes the attribution chain: on a signature-verified
  `checkout.session.completed`, joins `client_reference_id` → lead `funnel_event` → `product_id`
  (fallback: checkout `metadata.product_id`) and writes `metric_event(stage=paid, value=amount_total)`.
  Unattributable session → ack (200), no write. Idempotent on `source="stripe:<session_id>"`
  (app pre-check + a `unique` constraint backstop so a concurrent redelivery can't double-count).
- `api/private/qa.py` — the QA gate. `POST .../qa/{id}/smoke-test` (S2.7) records the pre-QA smoke
  verdict; `POST .../qa/{id}/launch-checklist` (S2.8) emits the deterministic launch checklist from
  real setup state and crosses `setup_done → qa`. At the gate: `POST .../qa/{id}/checklist` (S3.1)
  enqueues an Opus call that generates the click-through `qa_checklist_item` rows (202), `GET` lists
  them. **S3.2:** `PATCH .../qa/{id}/checklist/{item_id}` records a tester's `pass`/`fail` + comment
  (gated to `qa`), and `POST .../qa/{id}/go-live` crosses `qa → live` only when the checklist exists
  and every *blocking* item is `pass` (409 listing the offending ords otherwise; non-blocking fails
  never block).

### Scheduled crank (S4.1)

- `modules/crank/crank.py` — `enqueue_due_cranks`: one `crank` job per LIVE product whose cadence
  has elapsed since its last crank (due-ness is DB-side; no Python tz arithmetic on SQLite
  datetimes). `@handler("crank")` fans out one `generate` child `job_run` per enabled, autonomous,
  non-paused channel × applicable content type (`blog` for `ChannelType.BLOG`, `social` for
  `ChannelType.REDDIT`). Children carry `channel_id`/`content_type` for per-cell crash isolation;
  added (not committed) here — the worker commits them atomically with the crank's DONE status.

### Content generators — social + SEO blog (S4.2 + S4.3)

- `models/content_item.py` — `ContentItem` + `ContentItemStatus`: one row per generated piece,
  scoped by `(product_id, channel_id, content_type)`. Full pipeline state set (`generated →
  critic_passed/failed → guard_failed → scheduled → published → retracted`); nullable seam columns
  (`critic_*`, `idempotency_key`, `scheduled_for`, `published_at`, `external_url`, `error`) are
  pre-seeded so S4.3–S4.7 need no `ALTER TABLE` (no Alembic in v1).
- `modules/crank/generate.py` — `@handler("generate")`: budget-gated generate → critic+safety gate
  loop (TECH_SPEC §8.2). Loads product + brief + brand kit; fetches recent items for novelty; calls
  `generate_social_post` or `generate_blog_article`; validates the pillar; then calls
  `critique_content` (haiku tier, a different tier than the generator) to score quality 0–1 and
  decide safety — one call, not two passes. A safety failure hard-blocks (`guard_failed`); a score ≥
  `critic_score_threshold` (default 0.7) accepts (`critic_passed`); a low score regenerates up to
  `critic_max_regenerations` times (default 2), then skips+logs the last candidate
  (`critic_failed`). Exactly one `ContentItem` is persisted per cell with its final status, critic
  score, and critic notes. Injected `generate=`/`critique=` fns keep the handler testable without a
  network call. No commit here — the worker commits atomically with the job's DONE status + summed
  cost.
- `ai/client.py` additions — `SocialPost`, `BlogArticle`, and `CriticVerdict` structured-output
  models; `generate_social_post` / `generate_blog_article` using `claude-opus-4-8` (`GEN_MODEL`)
  with adaptive thinking and a novelty block; `critique_content` using `claude-haiku-4-5`
  (`CRITIC_MODEL`) — a lighter, independent tier so the reviewer doesn't share the writer's blind
  spots. Token caps: `GEN_SOCIAL_MAX_TOKENS=1000`, `GEN_BLOG_MAX_TOKENS=4000`,
  `CRITIC_MAX_TOKENS=600`.

v1 ports (verified free on the dev VPS): FastAPI `:8010`, dashboard `:3010` — see
`infra/deploy/PORTS.md`; run `infra/deploy/check-ports.sh` on the host before binding.
No Celery/Redis/Postgres in v1 (Phase B).
