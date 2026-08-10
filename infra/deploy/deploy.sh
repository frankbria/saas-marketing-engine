#!/usr/bin/env bash
# Deploy a release of the SaaS Marketing Engine (S0.5, #80). Run as root ON THE VPS, after
# provision.sh:
#
#   infra/deploy/deploy.sh [git-ref]        # default: main
#
# Idempotent by construction: it builds a fresh release directory, flips a symlink, and restarts
# two units. Re-running with the same ref converges on the same state. Aborts non-zero on a port
# conflict, a failed build, a bad nginx config, or a health check that never comes up — a deploy
# that half-succeeded is worse than one that refused.
#
# Pull-based on purpose (no GitHub Actions CD): this box runs an engine expected to operate
# unattended for >=2 weeks, and auto-deploying every merge into the middle of that is a liability.
set -euo pipefail

REPO_URL=https://github.com/frankbria/saas-marketing-engine.git
RELEASES_ROOT=/opt/sme
CURRENT_LINK=$RELEASES_ROOT/current
ENV_FILE=/etc/sme/sme.env
SME_USER=sme
REF="${1:-main}"
KEEP_RELEASES=5

[ "$(id -u)" -eq 0 ] || { echo "deploy: must run as root" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "deploy: $ENV_FILE missing — run provision.sh first" >&2; exit 1; }
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

say() { printf '\n== %s\n' "$1"; }
RELEASE="$RELEASES_ROOT/releases/$(date -u +%Y%m%d-%H%M%S)"

say "port check"
# The ports we are about to bind must be free *or* already ours. A restart re-binds a port this
# engine already holds, so stop our units first and re-check — otherwise every redeploy would
# abort on its own listener. PORTS.md explains why these are 8020/3020 and not 8010/3010.
systemctl stop sme-api.service sme-dashboard.service 2>/dev/null || true
CHECK_SCRIPT="$RELEASES_ROOT/current/infra/deploy/check-ports.sh"
[ -x "$CHECK_SCRIPT" ] || CHECK_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/check-ports.sh"
if ! "$CHECK_SCRIPT" "$SME_API_PORT" "$SME_DASHBOARD_PORT"; then
    echo "deploy: ABORTED — a port we need is held by another process (see PORTS.md)" >&2
    echo "deploy: previous release left in place; restart it with:" >&2
    echo "        systemctl start sme-api sme-dashboard" >&2
    exit 1
fi

say "fetch release $REF"
git clone --depth 1 --branch "$REF" "$REPO_URL" "$RELEASE" 2>&1 | tail -2
DEPLOYED_SHA="$(git -C "$RELEASE" rev-parse --short HEAD)"
echo "checked out $REF @ $DEPLOYED_SHA"

say "backend deps"
( cd "$RELEASE/backend" && /usr/local/bin/uv sync --frozen 2>&1 | tail -3 )

say "dashboard build"
# node 20 is this box's default; Next 16 needs the nvm node 24 that .nvmrc pins. Same PATH the
# dashboard unit uses at runtime, so build and runtime cannot drift.
( cd "$RELEASE/dashboard" \
  && PATH="$SME_NODE_BIN:$PATH" npm ci --no-audit --no-fund 2>&1 | tail -3 \
  && PATH="$SME_NODE_BIN:$PATH" npm run build 2>&1 | tail -5 )

say "ownership + symlink flip"
chown -R "$SME_USER:$SME_USER" "$RELEASE"
# Atomic swap: `ln -sfn` to a temp name then `mv -T` replaces the symlink in one rename, so the
# units never observe a missing `current` if this is interrupted.
ln -sfn "$RELEASE" "$CURRENT_LINK.tmp"
mv -Tf "$CURRENT_LINK.tmp" "$CURRENT_LINK"
echo "current -> $RELEASE"

say "start services"
systemctl start sme-api.service sme-dashboard.service

say "health check"
# Poll rather than sleep-and-hope: uvicorn boots the scheduler, runs init_db, and reclaims
# orphaned jobs before it serves, so readiness is not instant and is not a fixed duration.
HEALTH="http://127.0.0.1:$SME_API_PORT/health"
for attempt in $(seq 1 30); do
    if curl -fsS --max-time 2 "$HEALTH" >/dev/null 2>&1; then
        echo "api healthy after ${attempt}s"
        break
    fi
    if [ "$attempt" -eq 30 ]; then
        echo "deploy: FAILED — $HEALTH never became healthy" >&2
        echo "deploy: recent logs:" >&2
        journalctl -u sme-api.service -n 40 --no-pager >&2
        exit 1
    fi
    sleep 1
done

say "nginx reload"
# Generated vhosts point at the workspace, which has not moved — but the snippets they include may
# have been re-rendered by a provision.sh run. Always via the tested-first helper.
/usr/local/sbin/sme-nginx-reload

say "prune old releases"
# Keep the last N so rollback has somewhere to go (see RUNBOOK.md). Never prune `current`.
CURRENT_TARGET="$(readlink -f "$CURRENT_LINK")"
find "$RELEASES_ROOT/releases" -maxdepth 1 -mindepth 1 -type d | sort -r | tail -n +$((KEEP_RELEASES + 1)) \
    | while read -r old; do
        [ "$(readlink -f "$old")" = "$CURRENT_TARGET" ] && continue
        rm -rf "$old" && echo "pruned $(basename "$old")"
    done

cat <<EOF

Deployed $REF @ $DEPLOYED_SHA
  api        127.0.0.1:$SME_API_PORT
  dashboard  127.0.0.1:$SME_DASHBOARD_PORT

Verify:  $(dirname "${BASH_SOURCE[0]}")/verify-deploy.sh
EOF
