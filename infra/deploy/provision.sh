#!/usr/bin/env bash
# One-time host provisioning for the SaaS Marketing Engine (S0.5, #80). Run as root ON THE VPS.
#
#   sudo infra/deploy/provision.sh
#
# Idempotent: safe to re-run after a change to the units, the snippets, or the reload script.
# It creates the service user and directories, installs the nginx snippets + systemd units +
# the sudoers grant, and enables (but does not start) the services — `deploy.sh` starts them
# once there is actually a release to run.
#
# This box is shared with four other projects. Everything here ADDS files; nothing edits or
# removes an existing vhost, and the one nginx reload goes through `nginx -t` first.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/infra/deploy"

SME_USER=sme
SME_HOME=/srv/sme
RELEASES_ROOT=/opt/sme
ENV_FILE=/etc/sme/sme.env
ACME_WEBROOT=/var/www/acme
SNIPPETS_DIR=/etc/nginx/snippets
RELOAD_BIN=/usr/local/sbin/sme-nginx-reload

[ "$(id -u)" -eq 0 ] || { echo "provision: must run as root" >&2; exit 1; }

say() { printf '\n== %s\n' "$1"; }

say "service user + directories"
# --system: no login, no password, no home-dir clutter. The engine is a daemon, not a person.
if ! id "$SME_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$SME_HOME" --shell /usr/sbin/nologin "$SME_USER"
    echo "created user $SME_USER"
else
    echo "user $SME_USER already exists"
fi
install -d -o "$SME_USER" -g "$SME_USER" -m 755 "$SME_HOME" "$SME_HOME/workspace"
install -d -o "$SME_USER" -g "$SME_USER" -m 755 "$RELEASES_ROOT" "$RELEASES_ROOT/releases"
# 755 on the workspace, not 700: nginx (www-data) serves `workspace/<slug>/site` in place since
# S4.5.1/#78, so it needs traverse+read. The credentials vault is a SIBLING of site/, never
# beneath it (TECH_SPEC §11) — lock it down separately.
install -d -o "$SME_USER" -g "$SME_USER" -m 700 "$SME_HOME/vault"
install -d -o root -g root -m 750 /etc/sme

say "env file"
if [ ! -f "$ENV_FILE" ]; then
    install -o root -g "$SME_USER" -m 640 "$DEPLOY_DIR/sme.env.example" "$ENV_FILE"
    echo "installed $ENV_FILE from the example — FILL IN THE SECRETS BEFORE DEPLOYING"
else
    echo "$ENV_FILE already exists — left untouched (it holds live secrets)"
fi
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

say "ACME webroot"
# Shared across every domain the engine hosts, and outside every product workspace: certbot must
# write it, and the engine must never serve it as content.
install -d -o root -g www-data -m 755 "$ACME_WEBROOT" "$ACME_WEBROOT/.well-known" \
    "$ACME_WEBROOT/.well-known/acme-challenge"

say "node toolchain"
# The dashboard must run as `sme`, and nvm installs node under /root/.nvm — which is unreachable
# for any non-root user because /root is mode 700 (traversal needs +x on every path component, so
# permissive inner directories do not help). Copy the pinned major somewhere root-owned and
# world-readable instead of loosening /root, which would expose every other project's secrets on
# this shared box.
NODE_TARGET=/usr/local/lib/sme-node
WANT_MAJOR="$(tr -d 'v \n' < "$REPO_ROOT/.nvmrc")"
if [ -x "$NODE_TARGET/bin/node" ] && \
   [ "$("$NODE_TARGET/bin/node" -v | sed 's/^v\([0-9]*\).*/\1/')" = "$WANT_MAJOR" ]; then
    echo "node $("$NODE_TARGET/bin/node" -v) already installed at $NODE_TARGET"
else
    SRC=""
    # Prefer a system node of the right major; fall back to whatever nvm has.
    if command -v node >/dev/null && [ "$(node -v | sed 's/^v\([0-9]*\).*/\1/')" = "$WANT_MAJOR" ]; then
        echo "system node $(node -v) already matches .nvmrc — no copy needed"
        NODE_TARGET="$(dirname "$(dirname "$(command -v node)")")"
    else
        for candidate in /root/.nvm/versions/node/v"$WANT_MAJOR".*; do
            [ -x "$candidate/bin/node" ] && SRC="$candidate"
        done
        if [ -z "$SRC" ]; then
            echo "provision: no node v$WANT_MAJOR found (need it for the Next dashboard)." >&2
            echo "  Install one, e.g.:  nvm install $WANT_MAJOR" >&2
            echo "  then re-run this script." >&2
            exit 1
        fi
        rm -rf "$NODE_TARGET"
        cp -a "$SRC" "$NODE_TARGET"
        chown -R root:root "$NODE_TARGET"
        chmod -R a+rX "$NODE_TARGET"
        echo "installed node $("$NODE_TARGET/bin/node" -v) at $NODE_TARGET (from $SRC)"
    fi
fi
# Prove the service user can actually execute it — the whole point of this step.
if ! sudo -u "$SME_USER" "$NODE_TARGET/bin/node" -v >/dev/null 2>&1; then
    echo "provision: $SME_USER cannot execute $NODE_TARGET/bin/node — the dashboard unit will fail" >&2
    exit 1
fi
echo "verified: $SME_USER can execute node"

say "nginx snippets"
install -d -m 755 "$SNIPPETS_DIR" "$SNIPPETS_DIR/sme-tls"
install -o root -g root -m 644 "$DEPLOY_DIR/nginx/sme-acme.conf" "$SNIPPETS_DIR/sme-acme.conf"
# Render the API port into the public-API snippet. envsubst with an explicit variable list so a
# stray `$host`/`$remote_addr`/`$scheme` in the template survives — those are nginx variables and
# must reach the config verbatim.
SME_API_PORT="${SME_API_PORT:?SME_API_PORT must be set in $ENV_FILE}" \
    envsubst '${SME_API_PORT}' \
    < "$DEPLOY_DIR/nginx/sme-public-api.conf.template" \
    > "$SNIPPETS_DIR/sme-public-api.conf"
chmod 644 "$SNIPPETS_DIR/sme-public-api.conf"
echo "rendered sme-public-api.conf against port $SME_API_PORT"

say "nginx reload helper + sudoers"
install -o root -g root -m 755 "$DEPLOY_DIR/nginx-reload.sh" "$RELOAD_BIN"
# Validate into a temp file first: a malformed /etc/sudoers.d entry can break sudo for every user
# on a box four other projects depend on.
SUDOERS_TMP="$(mktemp)"
printf '%s ALL=(root) NOPASSWD: %s\n' "$SME_USER" "$RELOAD_BIN" > "$SUDOERS_TMP"
if visudo -cf "$SUDOERS_TMP" >/dev/null; then
    install -o root -g root -m 440 "$SUDOERS_TMP" /etc/sudoers.d/sme-nginx
    echo "installed /etc/sudoers.d/sme-nginx"
else
    rm -f "$SUDOERS_TMP"
    echo "provision: refusing to install an invalid sudoers file" >&2
    exit 1
fi
rm -f "$SUDOERS_TMP"

say "systemd units"
install -o root -g root -m 644 "$DEPLOY_DIR/systemd/sme-api.service" /etc/systemd/system/
install -o root -g root -m 644 "$DEPLOY_DIR/systemd/sme-dashboard.service" /etc/systemd/system/
systemctl daemon-reload
# Enable (start on boot) but do NOT start: there is no release checked out yet. deploy.sh starts
# them. Enabling now is what satisfies "nothing restarts the API after a reboot" (DoD-2).
systemctl enable sme-api.service sme-dashboard.service >/dev/null
echo "enabled sme-api + sme-dashboard (not started — run deploy.sh)"

say "nginx config test"
# Only a test here. provision.sh adds snippets that nothing includes yet, so there is nothing to
# reload for — and on a shared box an unnecessary reload is an unnecessary risk.
nginx -t

cat <<EOF

Provisioning complete.

Next:
  1. Fill in the secrets in $ENV_FILE (vault key, Anthropic, Stripe, ...).
  2. Deploy a release:  $DEPLOY_DIR/deploy.sh
  3. Verify:            $DEPLOY_DIR/verify-deploy.sh

See RUNBOOK.md for first-deploy, redeploy, and rollback.
EOF
