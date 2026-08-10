# VPS port map (v1)

Per TECH_SPEC §11: **check port conflicts before binding.** v1 binds two ports; SQLite
is a file (no port). Run `./check-ports.sh` on the host before starting services.

| Service          | Port  | Bind interface        |
|------------------|-------|-----------------------|
| FastAPI (uvicorn)| 8020  | loopback / private    |
| Next dashboard   | 3020  | loopback / private    |
| Flower (S5.0)    | 5555  | loopback / private    |

> **Ports changed in S0.5 (#80).** v1 planned 8010/3010; both were taken by other projects on the
> shared box before we ever bound them (see the survey below). The engine binds 8020/3020 now.

Phase B (S5.0) reuses the VPS's existing localhost PostgreSQL 16 (`:5432`) and Redis
(`:6379`) — reserved for this since v1 (below). Flower claims `:5555` for media-queue
visibility, loopback-only like the dashboard. Local dev uses different host ports on
purpose (postgres `5440`, redis `6390`, flower `5555`; see `infra/compose.dev.yml`).
The ephemeral GPU worker binds nothing here — it runs at the provider and connects OUT
to Redis (`infra/gpu-worker/README.md` for transport rules).

## Conflict check — Hostinger dev VPS (195.35.14.177)

**This box is shared with four unrelated projects.** Treat every port and every nginx vhost on it
as someone else's until proven otherwise.

Survey taken 2026-08-10 (`ss -ltnp`, `docker ps`, `ls /etc/nginx/sites-enabled`):

| Port | Owner | Note |
|------|-------|------|
| `:8010` | `narrative-staging-backend` container | **was our planned API port** |
| `:3010` | podcastfy `next-server` (dev.podcaststudiohub.me) | **was our planned dashboard port** |
| `:3011`, `:6381` | narrative-staging frontend + redis containers | |
| `:8000`, `:3002` | `/opt/auto-author` deployment | |
| `:14100`, `:14200` | dev.codeframe.sh | |
| `:5432`, `:6379` | PostgreSQL 16, Redis — **localhost, reused by us** (NFR-3) | |
| `:80`, `:443` | nginx 1.24 — 5 vhosts for the other projects | |
| **`:8020`, `:3020`, `:5555`** | **free — SME claims these** | |

The earlier note here ("from server memory, 2026-06-09: none of 8010, 3010, or 5555 is bound") was
true when written and silently rotted. That is the whole reason `check-ports.sh` exists and why
`deploy.sh` aborts on it rather than trusting this file.

Verify on the host before every deploy:

```bash
infra/deploy/check-ports.sh        # checks 8020 + 3020 + 5555 by default
```

Exit code is non-zero if any checked port is taken; `deploy.sh` aborts on that.

### Rules for a shared box

- **Never edit an existing vhost** under `/etc/nginx/sites-enabled/`. We only add files.
- **Never reload nginx without `nginx -t` first.** A bad config takes down four other projects'
  sites, not just ours. `infra/deploy/nginx-reload.sh` is the only sanctioned path and it always
  tests first.
- ufw allows 22/80/443 only. Binding the private surface to `127.0.0.1` plus that firewall **is**
  the NFR-1 boundary — there is no auth on the private API.

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

The **static site** half of each public vhost is generated, not hand-written: `deploy_site` emits
`<marketing_domain>.conf` under `SME_NGINX_SITES_ROOT` with `root` pointing at that product's
workspace site tree (`workspace/<slug>/site`), served in place — the crank publishes blog posts and
podcast episodes straight into it (S4.5.1/#78). nginx needs read access to the workspace; the
credentials vault is a sibling of `site/`, never beneath it. Merge the API `location` blocks above
into the generated vhost (or keep them in a shared snippet `include`d by it).

App-level defenses behind nginx (do not rely on nginx alone): per-(slug, IP) rate limiting,
strict request validation, per-product CORS scoped to each product's `marketing_domain`, and
stdlib HMAC verification of the Stripe signature. The private surface keeps its deploy-time
firewall — there is no auth in v1.
