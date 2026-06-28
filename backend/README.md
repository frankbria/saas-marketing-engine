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

v1 ports (verified free on the dev VPS): FastAPI `:8010`, dashboard `:3010` — see
`infra/deploy/PORTS.md`; run `infra/deploy/check-ports.sh` on the host before binding.
No Celery/Redis/Postgres in v1 (Phase B).
