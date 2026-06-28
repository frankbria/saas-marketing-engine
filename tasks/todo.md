# SaaS Marketing Engine — Working Plan

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
- Nova theming (gray/Hugeicons/Nunito Sans) applied via the mandated preset; if the preset command isn't viable in-sandbox, ship Next.js+TS+ESLint+Tailwind and complete Nova theming as a noted follow-up.

Acceptance criteria (from issue #1):
- [ ] backend/ (uv) + dashboard/ (Next.js Nova) per TECH_SPEC §2 layout
- [ ] FastAPI app boots with empty api/private and api/public routers wired
- [ ] Pre-commit hooks: ruff + black (py), lint + tsc (ts) — all pass
- [ ] CI runs backend + frontend tests on PR
- [ ] Feature-branch → PR → main flow documented in README
