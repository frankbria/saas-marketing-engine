# GPU media worker (ephemeral, S5.0)

The one Docker image the GPU provisioner boots at the provider (RunPod) when `media`
Celery jobs are pending. Torn down when the queue idles — no persistent GPU host, no idle
spend (TECH_SPEC Phase B decision, 2026-07-03).

## Build & register

```bash
# from the repo root
docker build -f infra/gpu-worker/Dockerfile -t <registry>/sme-media-worker:v1 .
docker push <registry>/sme-media-worker:v1
```

Create a RunPod **template** from the pushed image, set its env (below), and put the
template id in `backend/.env` as `SME_GPU_POD_TEMPLATE_ID`. The provisioner
(`backend/app/modules/media/provisioner.py`) creates pods from that template.

Template env:

| Var | Value |
|---|---|
| `SME_CELERY_BROKER_URL` | the VPS Redis, reachable per the transport rules below |

## Broker transport — the worker connects OUT to the VPS Redis

The VPS Redis binds localhost only (`infra/deploy/PORTS.md`) and **must never be exposed
raw to the internet** (issue #28). Two acceptable transports:

1. **Tailscale (preferred — already on the VPS).** Bake `tailscaled` into a derived image
   or use the provider's startup script with an ephemeral auth key; the broker URL is then
   `redis://<vps-tailscale-ip>:6379/0` with Redis `requirepass` set as defense in depth.
2. **TLS + password.** Redis 7 with `tls-port`, a real cert, `requirepass`, and the
   broker URL `rediss://:<password>@<host>:<port>/0`, firewalled to provider egress IPs
   where possible.

Either way the credential lives in the pod template env at the provider and in the vault
on the VPS — never in the repo or the DB (§9).

## Local smoke test

```bash
docker run --rm -e SME_CELERY_BROKER_URL=redis://host.docker.internal:6390/0 \
  sme-media-worker:v1 celery -A app.celery_app inspect ping
```

(Compose dev Redis is loopback:6390 — `infra/compose.dev.yml`.)
