# S5.0 — Phase B infrastructure (issue #28)

Adapted implementation plan (source: issue comment by frankbria, adapted 2026-07-03 after codebase exploration).

## Steps

### 1. Config + dependency foundation
- `backend/pyproject.toml`: add `celery[redis]`, `redis`, `psycopg[binary]` (Postgres driver).
- `backend/app/config.py` (SME_ prefix, typed fields w/ bounds per existing pattern):
  - `celery_broker_url` (default `redis://localhost:6379/0`)
  - `gpu_api_key: SecretStr | None` (already present in `.env` as `SME_GPU_API_KEY`)
  - `gpu_provider` (default `"runpod"`), `gpu_pod_template_id` / image ref settings
  - `gpu_idle_teardown_minutes` (default 10, ge=1)
  - `media_gpu_monthly_cap_cents` (default 0 = unlimited, per repo convention)
  - `gpu_pod_rate_cents_per_minute` (for spend estimation)
  - `media_provisioner_interval_seconds`
- Register `gpu_api_key` with `install_redaction()` so it is scrubbed from logs.
- Update `backend/.env.example`.
- Tests: config defaults + bounds.

### 2. Celery app + `media` queue
- `backend/app/celery_app.py`: Celery instance, broker from settings, `task_routes` → `media` queue, `task_acks_late=True`, retry policy (max 3, matching worker.py MAX_ATTEMPTS).
- `backend/app/modules/media/__init__.py` + `tasks.py`: a `media.probe` no-op media task (real video/podcast generation is S5.1/S5.2).
- Queue-depth helper (redis LLEN on the media queue) for the orchestration loop.
- Tests (`tests/test_media_queue.py`): routing unit tests; integration with **real Redis** (CI services block; local compose) — enqueue → depth increments → celery test worker on `-Q media` consumes → depth drains. Skipif Redis unreachable locally (mirrors skipif-on-missing-env idiom); CI always runs it.

### 3. GPU provisioner interface + RunPod implementation
- `backend/app/modules/media/provisioner.py`:
  - `GpuProvisioner` Protocol: `ensure_worker()`, `teardown()`, `status()`.
  - `RunPodProvisioner`: httpx against RunPod REST (create pod from pinned template/image, poll ready, terminate pod; verify destroyed).
  - `_build_provider()` factory seam (test idiom: hand-written fake injected via monkeypatch, like `_build_reddit`).
- API key read from settings only — never persisted to DB.
- Tests (`tests/test_gpu_provisioner.py`): `_FakeGpuProvider` recording calls; state transitions deterministic via injected clock.

### 4. Orchestration loop + spend guardrails + lease ledger
- `backend/app/models/gpu_lease.py`: `GpuLease(table=True)` — provider, pod_id, status, started_at, ended_at, cost_cents. Serves as observability record **and** pod-minutes ledger for the monthly cap.
- `backend/app/modules/media/orchestrator.py`: `run_provisioner_tick(session, provider, now)`:
  - depth > 0 and no live worker and under cap → `ensure_worker()`, open lease
  - depth == 0 and worker idle > `gpu_idle_teardown_minutes` → `teardown()`, close lease (verified destroyed = billing stopped)
  - `month_to_date_gpu_cost_cents(session, now)` pre-check; cap breach → refuse provisioning + `raise_alert("gpu_spend_cap", ...)` (S6.2 path)
  - tick must never raise out (heartbeat pattern)
- `backend/app/scheduler.py`: register `_media_provisioner_tick` interval job.
- `backend/app/main.py`: import media module (registration side-effect pattern, main.py L13-21).
- Tests (`tests/test_gpu_orchestrator.py`): boot-on-pending; no double-boot while live; idle teardown at fixed `NOW` + timedelta; cap refusal + alert via caplog; **cold-start end-to-end**: job enqueued with no worker → tick boots fake provider → job completes → idle → teardown fires (the AC integration test).

### 5. Postgres migration path
- `backend/app/db.py`: guard SQLite PRAGMA listener by dialect (`sqlite` only); gate `_backfill_additive_columns` to SQLite (fresh Postgres gets columns from `create_all`).
- `.github/workflows/ci.yml`: add `services:` block (postgres:16, redis:7); export `SME_TEST_POSTGRES_URL` + broker URL; suite exercises both.
- Tests (`tests/test_postgres_path.py`): skipif no `SME_TEST_POSTGRES_URL` — engine boot, `init_db()`, JobRun enqueue/claim round-trip, additive-backfill no-op, pragma listener not applied.
- Docs: `infra/POSTGRES_MIGRATION.md` — SQLite → Postgres data copy procedure.

### 6. Infra: compose, GPU worker image, ports
- `infra/compose.dev.yml` (canonical path per TECH_SPEC L99-101): redis (host port 6390), postgres (host port 5440), flower (5555, private interface). Non-default host ports avoid VPS collisions (VPS already has pg:5432/redis:6379 localhost-only).
- `infra/gpu-worker/Dockerfile` (first Dockerfile in repo): pinned `python:3.13-slim` + ffmpeg, installs backend + celery, `CMD celery -A app.celery_app worker -Q media --concurrency=1`. Connects OUT to VPS Redis (authenticated; transport = Tailscale, already on the VPS, or password+TLS — documented alongside, no raw internet Redis).
- `infra/deploy/PORTS.md` + `check-ports.sh`: claim 5555/6390/5440.

## Acceptance criteria → test map
- [ ] Celery + Redis `media` queue w/ retries + Flower → `test_media_queue.py` (real Redis) + flower service in compose
- [ ] SQLite → Postgres path exercised → `test_postgres_path.py` in CI (services block) + migration doc
- [ ] Provisioner boots pod on pending jobs / tears down on idle (verified destroyed) → `test_gpu_orchestrator.py`
- [ ] Cold-start tolerated end-to-end → cold-start integration test in `test_gpu_orchestrator.py`
- [ ] Provider-agnostic interface, one commercial impl, key in vault/env never DB → `provisioner.py` Protocol + RunPod impl + `SME_GPU_API_KEY` SecretStr + redaction
- [ ] Monthly cap + alert + teardown-on-idle integration test → orchestrator cap tests + caplog alert assertion

## Deviations from the issue-comment plan (autonomous decisions)
1. **Compose path**: `infra/compose.dev.yml`, not `infra/deploy/` — TECH_SPEC declares the canonical path.
2. **No celery-beat service**: no periodic Celery tasks exist yet (scheduling stays on APScheduler per the plan's own non-goals); beat added when the first periodic Celery task lands. YAGNI.
3. **Provisioning events in a new `gpu_lease` table, not `job_run`**: `job_run` rows are per-product; the GPU lease is global, and the lease table doubles as the pod-minutes ledger the monthly cap needs (mirrors `month_to_date_cost_cents` shape).
4. **GPU image ships worker + ffmpeg only**: ACE-Step/manim/Remotion pins arrive with S5.1/S5.2 (their versions would be guesswork now; image stays pinned + extensible).
5. **Provider key via env SecretStr** (`SME_GPU_API_KEY`), matching Anthropic/Stripe handling — the plan comment itself says "vault/env"; the Fernet vault is for per-product channel creds.
6. **Redis transport for remote worker**: password-auth Redis reached over Tailscale (already installed on the VPS) documented as the deployment path; code only consumes a broker URL.

## Non-goals (from issue)
- Migrating existing text/blog crank jobs to Celery
- Multi-provider failover
- Autoscaling beyond 0↔1 worker
