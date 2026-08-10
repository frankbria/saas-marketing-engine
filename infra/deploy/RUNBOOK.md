# Deploy runbook — SaaS Marketing Engine (S0.5, #80)

Target: the Hostinger dev VPS, `195.35.14.177` (`ssh staging`). Everything here runs **as root on
the box**.

> **This box is shared with four unrelated projects** — dev.autoauthor.app, dev.codeframe.sh,
> dev.briaanalytics.com, dev.podcaststudiohub.me. Two rules follow, and neither is optional:
> **never edit an existing nginx vhost**, and **never reload nginx without `nginx -t` first**. A
> broken reload is an outage for four teams who have nothing to do with this project.
> `nginx-reload.sh` is the only sanctioned reload path and it always tests first.

## Layout

| Path | What |
|---|---|
| `/opt/sme/releases/<UTC timestamp>` | one checked-out, built release |
| `/opt/sme/current` | symlink to the live release (both units run from it) |
| `/srv/sme/workspace` | per-product workspaces — **this is nginx's document root** (S4.5.1/#78) |
| `/srv/sme/sme.db` | SQLite (WAL) |
| `/etc/sme/sme.env` | environment + secrets, `root:sme` `0640` |
| `/etc/nginx/sme-sites/` | engine-generated vhosts (sme-owned; pulled in by `conf.d/sme-sites.conf`) |
| `/etc/nginx/snippets/sme-*.conf` | ACME + public-API includes |
| `/etc/nginx/snippets/sme-tls/<domain>/` | per-domain TLS, written by `enable-tls.sh` |
| `/usr/local/sbin/sme-nginx-reload` | the one command `sme` may run as root |
| `/usr/local/lib/sme-node` | node pinned by `.nvmrc`, readable by `sme` |

Ports: API `8020`, dashboard `3020`, Flower `5555` (reserved). **Not** 8010/3010 — those belong to
other projects on this box. See `PORTS.md`.

## First deploy

```bash
# 1. Bootstrap a checkout to run provisioning from.
git clone --depth 1 https://github.com/frankbria/saas-marketing-engine.git /tmp/sme-bootstrap

# 2. Provision the host. Idempotent — safe to re-run after changing units or snippets.
bash /tmp/sme-bootstrap/infra/deploy/provision.sh

# 3. Fill in the secrets. The vault key is generated once and MUST NOT be rotated afterwards —
#    every stored credential is encrypted with it and becomes undecryptable if it changes.
#    Generate one with:
#      uv run --with cryptography python -c \
#        'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
$EDITOR /etc/sme/sme.env

# 4. Deploy.
/tmp/sme-bootstrap/infra/deploy/deploy.sh main

# 5. Verify. Non-zero exit means something in the deploy is not what it claims to be.
/opt/sme/current/infra/deploy/verify-deploy.sh
```

## Redeploy

```bash
ssh staging /opt/sme/current/infra/deploy/deploy.sh main
```

Idempotent: builds a new release directory, flips `current`, restarts both units, and aborts
non-zero on a port conflict, a failed build, or a health check that never comes up. The previous
release is left in place, so a failure mid-deploy leaves a rollback target.

Deploy is **pull-based on purpose** — there is no GitHub Actions CD. The engine is expected to run
unattended for ≥2 weeks (DoD-2), and auto-deploying every merge into the middle of that is a
liability, not a convenience.

## Rollback

```bash
ls -1 /opt/sme/releases              # newest last; deploy.sh keeps the most recent 5
ln -sfn /opt/sme/releases/<older> /opt/sme/current.tmp
mv -Tf /opt/sme/current.tmp /opt/sme/current
systemctl restart sme-api sme-dashboard
/opt/sme/current/infra/deploy/verify-deploy.sh
```

The symlink swap is a single `rename(2)`, so the units never observe a missing `current`.

**Rollback does not undo a schema change.** v1 has no migration tooling by design (`app/db.py`);
`init_db` only adds tables/columns. Rolling back to a release that predates a new column is safe
(the column is ignored); rolling back across a *data* change is not. Check what changed first.

## TLS for a product domain

Prerequisites: the domain's DNS A record points at this box, and `setup_site` has already run for
that product (the generated vhost is what carries the ACME include).

```bash
/opt/sme/current/infra/deploy/enable-tls.sh acme.example
```

The script refuses to call certbot until it has *proved* the challenge path is served, so a
misconfiguration costs a local 404 rather than a failed validation against Let's Encrypt's
per-domain weekly rate limit.

**Why the ACME include exists at all:** #78 added `location ~ /\. { deny all; }` to every generated
vhost to hide atomic-write sidecars. That regex matches any path containing `/.` — including
`/.well-known/acme-challenge/`. Without the `^~` prefix location in `sme-acme.conf` taking
precedence, certbot's challenge is denied, validation times out, and the site silently stays
HTTP-only with nothing in the app logs.

Renewal is automatic: `certbot.timer` was already active on this box, and the `--deploy-hook`
reloads nginx after each renewal. Check with `systemctl list-timers | grep certbot` and
`certbot certificates`.

After enabling TLS, set `SME_PUBLIC_API_BASE_URL` and `SME_OAUTH_REDIRECT_BASE_URL` to the `https`
origin and redeploy — generated landing sites bake the API origin into their funnel JS at build
time, so an old value keeps being served until the site is rebuilt.

## Troubleshooting

**API won't start.**
```bash
systemctl status sme-api; journalctl -u sme-api -n 50 --no-pager
```
Most common: a bad value in `/etc/sme/sme.env`. Config validation is deliberately fail-loud at
startup (`config.py`), so a nonsense `critic_score_threshold` or a non-https
`SME_OAUTH_REDIRECT_BASE_URL` stops the process rather than silently disabling a guard.

**Deploy aborts on the port check.** Something else took 8020 or 3020. Find it with
`ss -ltnp | grep -E ':(8020|3020)'`. Do not "fix" this by moving to a port another project is
using — that is exactly how 8010/3010 were lost.

**A published page 404s.** Check the vhost's `root` is absolute and points into
`/srv/sme/workspace/<slug>/site`. `SME_WORKSPACE_ROOT` **must** be absolute: nginx resolves a
relative `root` against its own prefix (`/etc/nginx`), not the process's cwd. Note the workspace
path is baked into each vhost when `setup_site` runs, so relocating the workspace means re-running
`setup_site` for every product.

**Certificate issuance times out.** Almost always the dotfile deny above. Confirm the vhost
includes `sme-acme.conf`, then re-run `enable-tls.sh` — its preflight will tell you directly.

**`git` says "dubious ownership" in a release dir.** You are running git as root against a
checkout owned by `sme`. Harmless — `deploy.sh` reads the SHA before it chowns, so deploys are
unaffected. For ad-hoc inspection use `runuser -u sme -- git -C /opt/sme/current ...`.

**nginx reload fails.** `nginx -t` prints the offending file and line. The generated vhost is left
on disk deliberately, so fix the cause and run `/usr/local/sbin/sme-nginx-reload` by hand rather
than re-running a `setup_site` job that spends tokens.

## What this deployment deliberately does not include

- **No separate Celery worker unit.** Every `media.*` task routes to the `media` queue, which the
  *ephemeral rented GPU pod* consumes; the `default` queue has no producers. The in-process worker
  loop and every scheduler tick run inside the API process, so `sme-api.service` restarting covers
  them. A unit for a process with nothing to consume would be theatre.
- **No Flower.** `:5555` stays reserved. The S6.2 heartbeat digest is the operator's queue
  visibility surface; adding Flower means a new runtime dependency for a queue whose only consumer
  is a pod that exists for minutes at a time.
- **No GitHub Actions CD** — see "Redeploy".
