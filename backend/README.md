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
`secrets/`) is populated by later phase issues. `db.py` (SQLite/SQLModel) and `scheduler.py`
(APScheduler) land in S0.2.
