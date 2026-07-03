# VPS port map (v1)

Per TECH_SPEC §11: **check port conflicts before binding.** v1 binds two ports; SQLite
is a file (no port). Run `./check-ports.sh` on the host before starting services.

| Service          | Port  | Bind interface        |
|------------------|-------|-----------------------|
| FastAPI (uvicorn)| 8010  | firewalled / private  |
| Next dashboard   | 3010  | firewalled / private  |
| Flower (S5.0)    | 5555  | loopback / private    |

Phase B (S5.0) reuses the VPS's existing localhost PostgreSQL 16 (`:5432`) and Redis
(`:6379`) — reserved for this since v1 (below). Flower claims `:5555` for media-queue
visibility, loopback-only like the dashboard. Local dev uses different host ports on
purpose (postgres `5440`, redis `6390`, flower `5555`; see `infra/compose.dev.yml`).
The ephemeral GPU worker binds nothing here — it runs at the provider and connects OUT
to Redis (`infra/gpu-worker/README.md` for transport rules).

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

## Public funnel surface (S2.2)

One uvicorn process (`:8010`) serves both API surfaces. nginx is what makes the split
real on the wire: it must expose **only** the public funnel-ingest paths to the internet
and keep everything else (the private dashboard/operator API) on the allowlisted interface.

Internet-facing paths (and nothing else):
- `POST /api/funnel/{slug}/visit`
- `POST /api/funnel/{slug}/lead`
- `POST /api/stripe/webhook`

Private paths (`/api/private/*`, the Next dashboard `:3010`) stay firewalled — same as today.

Example nginx for the public vhost (landing sites + Stripe):

```nginx
# public vhost — only the funnel + stripe routes are proxied through
location ~ ^/api/(funnel/|stripe/webhook$) {
    proxy_pass http://127.0.0.1:8010;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;   # rate limiter reads the real client IP
}
# everything else on this vhost 404s — /api/private is never exposed here.
location /api/ { return 404; }
```

App-level defenses behind nginx (do not rely on nginx alone): per-(slug, IP) rate limiting,
strict request validation, per-product CORS scoped to each product's `marketing_domain`, and
stdlib HMAC verification of the Stripe signature. The private surface keeps its deploy-time
firewall — there is no auth in v1.
