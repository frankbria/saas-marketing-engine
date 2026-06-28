# SaaS Marketing Engine — Working Plan

## S0.4 — Encrypted credentials vault (Fernet) (#4, branch feat/s0.4-credentials-vault)
Self-authored plan (no plan comment). No architectural fork — schema pinned by TECH_SPEC §4,
crypto/redaction by §9. Single global key for v1 (ponytail: per-product keys deferred). TDD.

Acceptance criteria (issue #4):
- [ ] `credential` model; Fernet encrypt/decrypt with key from env `SME_VAULT_KEY` (not in DB)
- [ ] Write/read round-trips; only ciphertext at rest
- [ ] Plaintext never logged (lint rule + log redaction)
- [ ] Test asserts secret absent from captured logs

Steps (TDD):
1. [ ] dep: `cryptography` in backend/pyproject.toml.
2. [ ] config: `vault_key: str | None` (env `SME_VAULT_KEY`).
3. [ ] `app/secrets/vault.py`: Fernet `encrypt`/`decrypt`/`generate_key`; `put_credential`/`get_credential`;
       `SecretRedactingFilter` + `register_secret` + `install_redaction`; encrypt/decrypt register plaintext.
4. [ ] `app/models/credential.py`: §4 fields (id, product_id, channel_id nullable, key, ciphertext, expires_at, created_at); safe `__repr__`. Register in models `__init__`.
5. [ ] wire `install_redaction()` into main.py lifespan.
6. [ ] tests: roundtrip; ciphertext-at-rest (raw row); missing-key raises; redaction scrubs captured logs; static lint test scans `app/` for plaintext logging.

## Phase: Discovery → PRD → Spec (current)
- [x] Read BRAINSTORM.md transcript
- [x] Run structured brainstorm (3 decision rounds)
- [x] Write PRD.md
- [x] Write USER_STORIES.md
- [x] Frank filled inputs (Auto Author = github.com/frankbria/auto-author; no accounts/brand; autoauthor.app domain; dashboard no-auth/firewalled)
- [x] Write TECH_SPEC.md
- [x] Add G7 no-product-hardcoding rule (Auto Author is fixture only)
- [x] git init + push to github.com/frankbria/saas-marketing-engine (private, gh acct=frankbria verified)
- [x] Multi-perspective debate review (simplicity hawk vs functionality advocate)
- [x] Apply Revision 0.2 to PRD + TECH_SPEC (3 user decisions below)
- [x] Reflow PRD + TECH_SPEC + USER_STORIES bodies to inline all v0.2 deltas (no §0 supersession block; clean self-consistent docs, story IDs aligned across all three)
- [ ] Review/approve v0.2 docs
- [x] Break phases into atomic GitHub issues (#1–#34, 7 phase milestones, type labels; story IDs map 1:1 to issue numbers, S0.1=#1 … S6.4=#34)
- [ ] Start P0 (foundation) build — issues #1–#4

## S0.2 — Storage + scheduler + infra (#2, branch feat/s0.2-storage-scheduler-infra) ✅
Self-authored plan (no plan comment on issue). TDD. 11 tests pass; live round-trip demo'd.
- [x] deps: add `sqlmodel`, `apscheduler` to backend/pyproject.toml (no celery/redis/postgres)
- [x] `app/db.py`: SQLModel engine on SQLite, WAL + busy_timeout via PRAGMA on connect; `init_db()` (metadata.create_all — no alembic in v1), `get_session()`
- [x] `app/models/job_run.py`: JobRun table (id, product_id nullable [no FK — product table is S0.3], kind, status, attempts, token_cost_cents, started_at, finished_at, error, created_at)
- [x] `app/worker.py`: job handler registry + `enqueue()` + `run_due_jobs(session)` (sync, deterministic — increments attempts, retries up to MAX_ATTEMPTS, marks done/failed); `noop` handler
- [x] `app/scheduler.py`: APScheduler BackgroundScheduler — heartbeat enqueues noop + worker tick processes queue
- [x] wire into `main.py` lifespan: init_db + start/stop scheduler
- [x] `infra/deploy/check-ports.sh` + `PORTS.md`: port-conflict check (8010/3010) documented vs VPS (both free)
- [x] tests: WAL on, noop round-trips, retry-on-failure, transient-recover, unknown-kind, no celery/redis/postgres in deps

## S0.3 — Product registry model + API + onboarding form (#3, branch feature/issue-3-product-registry)
Self-authored plan (issue had acceptance criteria, no plan comment). No architectural fork:
schema pinned by TECH_SPEC §4, patterns by existing S0.1/S0.2 code. TDD.

Acceptance criteria:
- [x] `product` model per TECH_SPEC §4 (monetization_model default `cc_sub`, marketing_domain, token_budget_cents_month)
- [x] CRUD API (private router) + onboarding form (name, repo location, description, monetization model, domain, token budget)
- [x] New product → isolated workspace dir + empty credentials vault; lifecycle = `draft`
- [x] Product list view in dashboard
- [x] No operator login (firewalled — nothing to build)

Steps (TDD: test first):
1. [x] Config: `workspace_root` setting (`SME_WORKSPACE_ROOT`, default `./workspace`).
2. [x] `app/models/product.py`: `Product` table w/ all TECH_SPEC §4 fields; `MonetizationModel`+`LifecycleState` StrEnums; defaults cc_sub/draft; `slug` unique-indexed. Register in models `__init__`.
3. [x] `app/workspace.py`: `create_workspace(slug)` makes `{root}/{slug}/` + `{slug}/vault/` (empty cred vault); idempotent. `remove_workspace(slug)`.
4. [x] `app/api/private/products.py`: CRUD (POST slugifies+creates row+workspace+lifecycle=draft, GET list, GET {id}, PATCH {id}, DELETE {id}); pydantic create/update/read; wire into private `__init__`.
5. [x] `dashboard/lib/api.ts`: typed fetch wrapper (base from `NEXT_PUBLIC_API_BASE_URL`).
6. [x] `app/products/page.tsx` (list) + `app/products/new/page.tsx` (form); native Tailwind inputs + Button.

Review fixes (codex cross-family pass):
- [x] P1 CORS: dashboard origin calls private API cross-origin → added config-driven CORSMiddleware (`SME_CORS_ORIGINS`, default localhost:3010) + test.
- [x] P2 lifecycle: dropped `lifecycle_state` from PATCH (transitions belong to the state machine, S1.4/S3.2) + guard test.
- Verified live: 2 products created, workspace+vault on disk, G7 second product, delete removes workspace, lifecycle PATCH ignored. 27 backend + 6 frontend tests pass; build clean.

Deviations / assumptions:
- Vault in S0.3 = empty `vault/` dir; Fernet + `credential` table is S0.4.
- Native styled inputs over 5 new shadcn primitives (smaller diff, internal firewalled tool).
- DELETE included ("CRUD"); also removes workspace dir.
- brand_json/pricing fields present-but-nullable (folded per §4, populated by S1/S2).

## GitHub setup
- Milestones: Phase 0–6 (4/4/8/2/9/3/4 issues)
- Labels: backend, frontend, infra, devops, ai, integration, security
- Issue→story map is sequential: #1=S0.1, #5=S1.1, #9=S2.1, #17=S3.1, #19=S4.1, #28=S5.0, #31=S6.1
- Dependencies expressed by story ID in each issue body (GitHub has no native hard deps)

## Revision 0.2 decisions (2026-06-28 design review)
- Infra: SQLite(WAL) + APScheduler + job_run for v1; Celery/Postgres/Redis/Flower → Phase B only
- Cost: AI tokens = real metered spend, per-product budget + hard stop; Phase B media needs GPU (not on dev VPS) → text-only until separate decision
- Channels: owned-first — blog + email autonomous, Reddit warmed/careful, X/IG/YouTube deferred/human-assisted; drop browser fallback in v1
- Must-fix bug: split public funnel-ingest API from private dashboard API
- Must-add (cheap): attribution chain (UTM→cookie→lead→Stripe→webhook), heartbeat+alerts (zero-reach/shadowban), publish idempotency+novelty, adapter delete()/retract, pre-QA site smoke test, SPF/DKIM/DMARC, rate pacing, OAuth refresh handling
- Guardrail: one LLM critic {score,safety_pass,notes} + non-LLM blocklist + claim-traces-to-brief + first-item/random-10% human spot-check; generator≠critic tier
- Simplify: cc_sub only (keep enum), one site template + AI copy (not bespoke), single welcome email, brand_kit/pricing → JSON on product

## Locked decisions (from brainstorm 2026-06-28)
- Single-owner, multi-product (NOT multi-tenant — no auth/account isolation)
- Product #0: Auto Author · B2C/small-business first · B2E deferred
- Pipeline: Strategy → Setup → [human QA gate] → Crank (autonomous)
- Human-in-loop ONLY at: account/payment/domain setup + pre-launch QA
- Crank fully autonomous: generator→critic quality gate, no per-post approval
- Channels: landing site + IG/X/Reddit/YouTube (multi-channel)
- Content types: text/social + SEO blog (Phase A), short-form video + podcast (Phase B)
- Publishing: API-first, browser-automation fallback
- Monetization: configurable per product (CC-upfront sub / trial / freemium)
- Runtime: FastAPI + Next.js (Nova) dashboard
- Infra: Postgres + Celery + Redis + Flower on Hostinger dev VPS
- Landing sites: AI-bespoke per product + standardized funnel contract (Stripe/email/analytics components)
- Done-state: operator onboards a NEW product through full cycle with zero code changes; crank runs unattended ≥2 weeks with metrics flowing

## Open risks / to verify at build time
- VPS port conflicts (FastAPI/Next/Postgres/Redis/Celery/Flower) — check before binding
- CORS (dashboard ↔ API) before first remote deploy
- Platform API access + ToS for autonomous posting (X/Reddit/YouTube/IG) on own accounts
- Bespoke-site QA cost vs standardized funnel contract — keep the contract strict
- 1-month timeline is aggressive for all 4 content types — Phase B may slip; that's fine

---

## Issue #1 — S0.1 Monorepo scaffold (feature/issue-1-monorepo-scaffold)
Self-authored plan (no architectural fork). TDD where meaningful (app boot + router wiring).

Steps:
1. [ ] Backend `uv` project: pyproject (fastapi/uvicorn/pydantic-settings + dev: pytest/httpx/ruff/black); `app/` package skeleton per TECH_SPEC §2 (api/private, api/public, models, modules/{strategy,setup,qa,crank,metrics}, channels, ai, secrets); `app/main.py` create_app mounting both routers + `/health`; `app/config.py` Settings. **RED**: test app boots + both router prefixes mounted + /health 200.
2. [ ] `dashboard/` Next.js Nova (Nova preset via shadcn; fallback create-next-app + nova init); trivial smoke test; lint + tsc pass.
3. [ ] `.pre-commit-config.yaml`: ruff + black (py), eslint + tsc (ts).
4. [ ] `.github/workflows/ci.yml`: backend pytest + frontend lint/tsc/test on PR to main.
5. [ ] `README.md`: project intro + feature-branch → PR → main flow + how to run/test.

Deviations / assumptions:
- `db.py` (needs SQLModel) and `scheduler.py` (APScheduler) are deferred to **S0.2** — they carry behavior, not layout; S0.1 ships the bootable skeleton + wired routers.
- Backend deps limited to what the scaffold uses (no SQLModel/Celery/etc. yet) — YAGNI.
- Nova applied via the named `nova` shadcn preset (the mandated preset **URL** returns registry 400, so the URL-encoded gray/Hugeicons/Nunito-Sans choices were applied **manually** after scaffolding: removed lucide-react → @hugeicons/react, font → Nunito Sans, baseColor → gray, dark sidebar-primary normalized to gray). The plain Next.js+TS+ESLint+Tailwind fallback was **not** needed and is not an acceptable end state.

Acceptance criteria (from issue #1):
- [ ] backend/ (uv) + dashboard/ (Next.js Nova) per TECH_SPEC §2 layout
- [ ] FastAPI app boots with empty api/private and api/public routers wired
- [ ] Pre-commit hooks: ruff + black (py), lint + tsc (ts) — all pass
- [ ] CI runs backend + frontend tests on PR
- [ ] Feature-branch → PR → main flow documented in README
