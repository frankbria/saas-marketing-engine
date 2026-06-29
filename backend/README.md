# SME Backend

FastAPI backend for the SaaS Marketing Engine. Two API surfaces (TECH_SPEC §1):

- **private** (`/api/private/*`) — dashboard/operator API, firewalled, no auth in v1
- **public** (`/api/public/*`) — funnel-ingest (visit/lead) + Stripe webhook, internet-facing

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
- `api/private/strategy.py` — `POST /api/private/strategy/{product_id}/brief` and
  `POST .../{product_id}/brand` enqueue their jobs (202; `brand` 400s until a brief exists). The
  real-API integration tests are gated on `SME_ANTHROPIC_API_KEY`.

v1 ports (verified free on the dev VPS): FastAPI `:8010`, dashboard `:3010` — see
`infra/deploy/PORTS.md`; run `infra/deploy/check-ports.sh` on the host before binding.
No Celery/Redis/Postgres in v1 (Phase B).
