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
  `channel_id`/`content_item_id` (those tables arrive in P4), `stage`
  (`impression|visit|signup|paid`), `value` (cents for `paid`), `occurred_at`, and a `source`
  provenance/idempotency key (`unique`).
- `api/public/stripe.py` (S2.5) — closes the attribution chain: on a signature-verified
  `checkout.session.completed`, joins `client_reference_id` → lead `funnel_event` → `product_id`
  (fallback: checkout `metadata.product_id`) and writes `metric_event(stage=paid, value=amount_total)`.
  Unattributable session → ack (200), no write. Idempotent on `source="stripe:<session_id>"`
  (app pre-check + a `unique` constraint backstop so a concurrent redelivery can't double-count).

v1 ports (verified free on the dev VPS): FastAPI `:8010`, dashboard `:3010` — see
`infra/deploy/PORTS.md`; run `infra/deploy/check-ports.sh` on the host before binding.
No Celery/Redis/Postgres in v1 (Phase B).
