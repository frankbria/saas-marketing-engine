# VPS port map (v1)

Per TECH_SPEC §11: **check port conflicts before binding.** v1 binds two ports; SQLite
is a file (no port). Run `./check-ports.sh` on the host before starting services.

| Service          | Port  | Bind interface        |
|------------------|-------|-----------------------|
| FastAPI (uvicorn)| 8010  | firewalled / private  |
| Next dashboard   | 3010  | firewalled / private  |

## Conflict check — Hostinger dev VPS (195.35.14.177)

Existing services on the box (from server memory, 2026-06-09): nginx `:80`,
PostgreSQL 16 `:5432` (localhost), Redis `:6379` (localhost), supervisor, tailscaled.
**Neither 8010 nor 3010 is bound** → both free for v1. Postgres/Redis are present but
unused in v1 (Phase B only).

Verify on the host before first deploy:

```bash
infra/deploy/check-ports.sh        # checks 8010 + 3010
```

Exit code is non-zero if either port is taken; the deploy script should abort on that.
