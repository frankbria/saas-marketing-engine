# S0.5 — VPS deploy automation (issue #80)

**Branch:** `feature/issue-80-vps-deploy-automation` · **Plan source:** self-authored from the
issue's acceptance criteria + a live survey of the target box (the issue has AC but no step plan).

## What is actually missing

`infra/deploy/` holds only `PORTS.md` and `check-ports.sh`. There is no CD workflow, no systemd
unit, no provisioning or release script, no TLS story, and no runbook. `deploy_site()` emits an
HTTP-only vhost and explicitly defers `nginx -s reload`, TLS, and remote copy as "operational"
(`backend/app/modules/setup/site.py`). Every deploy is manual SSH, and nothing restarts the API
after a reboot — which is the practical blocker between "CI is green" and "S6.4 (#34) can start".

## Survey of the target box (195.35.14.177, 2026-08-10)

The box is **shared with four unrelated projects**. This is the dominant constraint.

| Fact | Consequence |
|---|---|
| `:8010` → narrative-staging backend container; `:3010` → podcastfy `next-server` | **`PORTS.md` is stale.** Its "all three free" note is from 2026-06-09. |
| `:8020`, `:3020`, `:5555` free | SME's new ports. |
| nginx 1.24, 5 vhosts serving dev.autoauthor.app / dev.codeframe.sh / dev.briaanalytics.com / dev.podcaststudiohub.me | **Never edit an existing vhost. Never reload without `nginx -t` first** — a bad config takes down four other projects. |
| certbot installed, `certbot.timer` active, 4 live certs | Renewal infrastructure already exists; we add a deploy hook, not a timer. |
| ufw allows only 22/80/443 | Loopback binding + ufw *is* the NFR-1 private boundary. |
| Postgres 16 + Redis on localhost; `uv` at /usr/local/bin; node 20 default but **24.13.0 under nvm** | Reuse per NFR-3. The dashboard build needs the nvm node 24 path, not the default. |
| `/opt/auto-author` uses `releases/<ts>` + a `current` symlink | Mirror the box's existing release convention. |

## Decisions taken (forced moves, not architectural forks)

1. **Ports → 8020 / 3020 / 5555.** 8010/3010 are occupied, NFR-3 forbids new infra, and evicting
   other projects is not on the table. `check-ports.sh` defaults move too, or it aborts every
   deploy forever on ports we deliberately abandoned.
2. **Pull-based deploy script, no GitHub Actions CD.** The AC asks for an *idempotent deploy path*,
   not a CD workflow. Auto-deploying on every merge to a box mid-way through an unattended ≥2-week
   DoD run is a liability, and a push-based workflow needs a deploy-key secret we do not have.
   `ssh staging /opt/sme/current/infra/deploy/deploy.sh` is the entry point.
3. **TLS via a wildcard `include` that survives vhost regeneration.** `deploy_site` rewrites
   `<domain>.conf` on every `setup_site` run, so anything certbot's `--nginx` plugin injected there
   would be silently wiped on the next run. Instead the generated vhost carries
   `include .../sme-tls/<domain>/*.conf;` — a *wildcard* include, which nginx tolerates when it
   matches nothing — and `enable-tls.sh` writes the cert directives into that directory.
4. **ACME challenge before the dotfile deny.** `location ^~ /.well-known/acme-challenge/` — a
   prefix match outranks the `location ~ /\.` regex deny that #78 added, which would otherwise
   silently fail every certificate issuance (the failure mode called out in the issue comment).
5. **No separate Celery unit.** The AC says "(Phase B) the Celery worker", but there is no
   VPS-side Celery worker to run: `task_routes` sends every `media.*` task to the `media` queue,
   which the *ephemeral GPU pod* consumes, and `default` has no producers. The in-process worker,
   scheduler, publish/crank/render ticks all live inside the API process (`app/scheduler.py`), so
   `sme-api.service` restart-on-failure already covers "the worker". Shipping a unit for a process
   with nothing to consume would be theatre. Documented in the runbook + PR rather than silently
   skipped.

## Steps

**A. Repo artifacts (no live-box changes — safe to land regardless)**

1. `PORTS.md` — replace the stale conflict section with the 2026-08-10 survey; new port table.
2. `check-ports.sh` — defaults `8020 3020 5555`.
3. `sme.env.example` — deployment env template (absolute `SME_WORKSPACE_ROOT`, ports, CORS origin,
   public API base URL, nginx roots, reload command, node bin).
4. `systemd/sme-api.service`, `systemd/sme-dashboard.service` — loopback bind, `Restart=always`,
   `EnvironmentFile`, non-root `User=sme`.
5. `nginx/sme-acme.conf`, `nginx/sme-public-api.conf.template` — the ACME prefix location, and the
   funnel/stripe proxy with `location /api/ { return 404; }` for everything else.
6. `nginx-reload.sh` — `nginx -t && systemctl reload nginx`. The single sudoers-allowed command, so
   the service user can reload nginx without broader root.
7. `provision.sh` — one-time, idempotent host setup: user, dirs, ACME webroot, snippets, units,
   sudoers (validated with `visudo -c`).
8. `deploy.sh` — idempotent release: port check from env, fetch/checkout, `uv sync`, dashboard
   build with the nvm node 24 path, restart units, guarded nginx reload, health check.
9. `enable-tls.sh <domain>` — `certbot certonly --webroot`, write the per-domain TLS snippet,
   guarded reload; idempotent (skips a live cert).
10. `verify-deploy.sh` — asserts the private surface is loopback-only and CORS answers for the real
    origin (AC 6 + 7 folded into one script rather than two).
11. `RUNBOOK.md` — first deploy, redeploy, rollback, TLS, troubleshooting.

**B. Backend changes (TDD — tests first)**

12. `config.py` — `nginx_snippets_root`, `nginx_reload_command` (default `""` = disabled, so dev
    and tests are unaffected).
13. `site.py` `deploy_site` — emit the three `include` lines; run the reload command when
    configured. Failure to reload must surface, not pass silently.
14. Tests for the includes + the reload seam.

**C. Live verification (mutating — confirm before starting)**

15. `provision.sh` + `deploy.sh` against the real box, then `verify-deploy.sh`, the ACME-path
    proof, and the restart-on-failure proof.

16. `TECH_SPEC.md` §11 — bring into line with reality.

## Acceptance criteria (from #80)

- [x] systemd units for the uvicorn API and the worker, with restart-on-failure —
      `kill -9` pid 663235, systemd restarted it as 663473, `/health` ok; both units `enabled`
- [x] Idempotent deploy path; aborts non-zero if `check-ports.sh` fails — two consecutive runs
      rc=0 and healthy; with a squatter on 3020 the run exits **1**, names the port, creates no
      release
- [x] nginx base config: `/api/private/products` returns **200 direct / 404 through nginx**;
      `/api/funnel/<real slug>/visit` returns **201 both ways**; `/api/stripe/webhook` reaches
      the app (400 on an empty body)
- [x] TLS documented + automated (`enable-tls.sh`); the ACME challenge path is **proven served
      past the #78 dotfile deny** (`/.well-known/acme-challenge/proof.txt` returns its content
      while `/.hidden/secret` still 403s). Issuance itself is untested — no domain resolves here
- [x] `nginx -s reload` wired to site deploy — `deploy_site` as `sme` wrote the vhost and
      reloaded via the sudoers grant; the page then served 200 from the workspace tree
- [x] Private surface verified loopback-bound — listeners are `127.0.0.1:8020/3020`, and both
      ports time out from off-box
- [x] CORS verified against the real origin — `verify-deploy.sh` preflight
- [x] Runbook covering first deploy, redeploy, rollback, TLS, troubleshooting

## Defects found by actually deploying (all fixed)

Eight, none of which a file review would have caught. Seven are one pattern — **privilege and path
assumptions that only fail when a different user runs the code**:

1. `StartLimitIntervalSec`/`StartLimitBurst` in `[Service]` — systemd ignores them there with only
   a log warning, so the crash-loop throttle silently did not exist.
2. `/usr/local/bin/uv` is a symlink into `/home/podcastfy/.local/bin` (0750) — a per-user install
   wearing a system-path costume.
3. Building as root put the venv's interpreter under `/root/.local/share/uv` (0700) → `203/EXEC`
   with every file on disk looking correct. **Fixed structurally: build user == runtime user.**
4. `provision.sh` preserved the live env file (correct — secrets) but therefore never delivered
   newly-required keys → `unbound variable` on the second provision.
5. `ExecStart=${VAR} ...` — systemd forbids a variable as the *first* token, failing with the same
   `203/EXEC` as a wrong path.
6. `sme` cannot write root-owned `/etc/nginx/sites-enabled`; `ReadWritePaths=` relaxes systemd's
   sandbox but grants no filesystem permission.
7. `/etc/sme` was `root:root` 0750, so `sme` could not traverse to the env file its own group
   ownership promised it could read.

8. **`NoNewPrivileges=true` blocked the API's one privileged action** — found by the GLM review,
   not by me. `deploy_site` shells out to `sudo /usr/local/sbin/sme-nginx-reload`; the kernel
   refuses every setuid exec under that flag, so `setup_site` would have errored on every retry
   while the unit looked *more* hardened and `/health` stayed green. **My demo could not have
   caught it**: I exercised `deploy_site` via `runuser`, which does not apply the unit's sandbox,
   so I proved a path production never takes. `verify-deploy.sh` now runs the real reload command
   as `sme` under the unit's own `NoNewPrivileges` setting.

And one weakness in my own verification: `verify-deploy.sh` asserted "funnel is proxied" from a
status code, but nginx-blocked and app-not-found both return 404 — the check passed identically
whether the allowlist worked or nginx swallowed everything. Now discriminates on the response body.

## Known limitations (for the PR)

- **No real certificate can be issued yet.** TLS issuance needs a domain whose DNS points at the
  box; no product has a `marketing_domain` configured. `enable-tls.sh` is therefore demonstrated up
  to the challenge path (serving `/.well-known/acme-challenge/` through nginx past the dotfile
  deny — the exact failure this issue's comment predicted), not through to a minted cert.
- No GitHub Actions CD workflow — see decision 2.
- Flower is not installed (`:5555` stays reserved). The S6.2 heartbeat digest is the operator's
  queue-visibility surface; adding Flower would mean a new runtime dependency for a queue whose
  only consumer is an ephemeral pod.
