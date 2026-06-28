# SaaS Marketing Engine (SME)

A single-owner system that takes a finished SaaS codebase and **autonomously stands up and
operates the marketing + monetization around it** — landing site, payment funnel, content, and
metrics — with humans only at two gates (account/payment/domain setup; pre-launch QA).

See [`PRD.md`](PRD.md), [`TECH_SPEC.md`](TECH_SPEC.md), and [`USER_STORIES.md`](USER_STORIES.md)
for the full design. Work is tracked as GitHub issues (`S0.1`–`S6.4`) across phase milestones P0–P6.

## Layout

```
backend/        FastAPI app — private dashboard API + public funnel API (uv, Python 3.13)
dashboard/      Next.js (Nova: gray, Hugeicons, Nunito Sans) operator dashboard
site-template/  landing-site template (added in Phase 2 / S2.1)
infra/          deploy + dev infra (added as needed)
PRD.md · TECH_SPEC.md · USER_STORIES.md · tasks/todo.md
```

## Develop

```bash
# backend
cd backend && uv sync
uv run uvicorn app.main:app --reload --port 8010   # http://localhost:8010/health
uv run pytest

# dashboard
cd dashboard && npm install
npm run dev                                         # http://localhost:3010
npm run test && npm run typecheck && npm run lint
```

Install hooks once: `pre-commit install` (runs ruff + black on backend, eslint + tsc on dashboard).

## Contributing flow

1. Branch off `main`: `git checkout -b feature/issue-<N>-<slug>`.
2. Implement with TDD (tests first); keep commits conventional (`feat(scope): …`).
3. Pre-commit hooks must pass; push and open a **PR to `main`**.
4. CI (`.github/workflows/ci.yml`) runs backend + frontend checks on the PR.
5. Merge after CI is green and review is addressed. `main` is the release branch.
