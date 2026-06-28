# SME Dashboard

Next.js (Nova theme — radix base, **gray** palette, **Hugeicons**, **Nunito Sans**) operator
dashboard for the SaaS Marketing Engine. Talks to the backend's **private** API
(`/api/private/*`). VPS-firewalled, no auth in v1 (NFR-1).

## Develop

```bash
npm install
npm run dev          # http://localhost:3010
npm run test         # vitest
npm run typecheck    # tsc --noEmit
npm run lint         # eslint
npm run build        # production build
```

Use Node 24 (`.nvmrc` at repo root) so `package-lock.json` stays in sync with CI.

## Conventions (per project standard)

- Icons: **`@hugeicons/react`** only — never `lucide-react`.
- Font: Nunito Sans (sans), Geist Mono (mono). Base color: gray.
- shadcn/Nova components live in `components/ui/`; add via `npx shadcn@latest add <name>`.

Dashboard screens (onboarding, strategy review, QA checklist, metrics) are built in later phase
issues (S0.3, S1.4, S3.2, S6.x).
