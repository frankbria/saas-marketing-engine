# SaaS Marketing Engine — Working Plan

## Phase: Discovery → PRD (current)
- [x] Read BRAINSTORM.md transcript
- [x] Run structured brainstorm (3 decision rounds)
- [x] Write PRD.md
- [x] Write USER_STORIES.md
- [ ] Frank fills "Inputs Needed" (Auto Author description + repo location)
- [ ] Review/approve PRD + user stories
- [ ] Generate technical spec from approved PRD
- [ ] Break phases into atomic GitHub issues (dependency-ordered)

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
