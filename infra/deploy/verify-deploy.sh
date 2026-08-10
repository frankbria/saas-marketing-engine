#!/usr/bin/env bash
# Post-deploy assertions for the SaaS Marketing Engine (S0.5, #80). Run as root ON THE VPS after
# deploy.sh:
#
#   infra/deploy/verify-deploy.sh [public-host]
#
# Covers the two acceptance criteria that are claims about the *running* system rather than about
# files: the private surface is not reachable from the internet (NFR-1), and CORS answers for the
# real dashboard origin (NFR-2). Exits non-zero if any assertion fails.
#
# NFR-1 ships no auth on /api/private/*, so "is it firewalled" is not a hardening nicety — the
# firewall and the loopback bind ARE the authorization model. It gets asserted on every deploy.
set -euo pipefail

ENV_FILE=/etc/sme/sme.env
[ -f "$ENV_FILE" ] || { echo "verify: $ENV_FILE missing — run provision.sh first" >&2; exit 1; }
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

PUBLIC_HOST="${1:-}"
FAILED=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILED=1; }
say()  { printf '\n== %s\n' "$1"; }

say "services running"
for unit in sme-api sme-dashboard; do
    if systemctl is-active --quiet "$unit"; then
        pass "$unit is active"
    else
        fail "$unit is NOT active"
    fi
done

say "private surface is loopback-only"
# The check is on the BIND ADDRESS, not on whether a remote connection happens to fail right now.
# A service bound to 0.0.0.0 that is currently unreachable is one firewall-rule edit away from
# being exposed; a service bound to 127.0.0.1 cannot be exposed by a firewall mistake at all.
for port in "$SME_API_PORT" "$SME_DASHBOARD_PORT"; do
    BIND="$(ss -ltnH "( sport = :$port )" | awk '{print $4}' | head -1)"
    case "$BIND" in
        127.0.0.1:*|\[::1\]:*) pass "port $port bound to loopback ($BIND)" ;;
        "")                    fail "port $port has no listener" ;;
        *)                     fail "port $port bound to $BIND — expected 127.0.0.1" ;;
    esac
done

say "firewall"
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    pass "ufw is active"
    if ufw status | grep -Eq "^($SME_API_PORT|$SME_DASHBOARD_PORT)(/tcp)?[[:space:]]+ALLOW"; then
        fail "ufw has an explicit ALLOW for a private port — it must not be reachable"
    else
        pass "no ufw ALLOW rule exposes $SME_API_PORT/$SME_DASHBOARD_PORT"
    fi
else
    fail "ufw is not active — the private surface has no boundary (NFR-1)"
fi

say "the API service can actually reload nginx"
# Not a hypothetical. `deploy_site` shells out to SME_NGINX_RELOAD_COMMAND from inside the API
# process, so if the unit's sandbox blocks that escalation, every generated vhost is written and
# never served — and the unit looks *more* hardened while the crank silently fails. This assertion
# exists because that regression is invisible from outside: the service is active, /health is ok,
# and only a real setup_site run would reveal it.
NNP="$(systemctl show sme-api -p NoNewPrivileges --value 2>/dev/null)"
case "$SME_NGINX_RELOAD_COMMAND" in
    *sudo*)
        if [ "$NNP" = "yes" ]; then
            fail "sme-api has NoNewPrivileges=yes but its reload command uses sudo — the kernel will refuse it"
        else
            # Exercise it for real under the service's own uid rather than trusting the property.
            if systemd-run --quiet --uid=sme --property=NoNewPrivileges="$NNP" --wait --collect \
                --pipe /bin/sh -c "$SME_NGINX_RELOAD_COMMAND" >/dev/null 2>&1; then
                pass "the service user can run the nginx reload under the unit's sandbox"
            else
                fail "the reload command failed when run as sme under the unit's sandbox"
            fi
        fi
        ;;
    "") fail "SME_NGINX_RELOAD_COMMAND is empty — deploy_site would write vhosts and never serve them" ;;
    *)  pass "reload command needs no escalation ($SME_NGINX_RELOAD_COMMAND)" ;;
esac

say "api health"
if curl -fsS --max-time 5 "http://127.0.0.1:$SME_API_PORT/health" | grep -q '"ok"'; then
    pass "GET /health returns ok"
else
    fail "GET /health did not return ok"
fi

say "CORS for the dashboard origin (NFR-2)"
# The private API is called cross-origin from the operator's browser (different port), so a
# missing/incorrect ACAO header silently breaks the whole dashboard with only a console error to
# show for it. Verified against the origin from the env file, not a guess.
ORIGIN="${SME_CORS_ORIGINS%%,*}"
ACAO="$(curl -sS -o /dev/null -D - --max-time 5 \
    -X OPTIONS "http://127.0.0.1:$SME_API_PORT/api/private/products" \
    -H "Origin: $ORIGIN" \
    -H "Access-Control-Request-Method: GET" 2>/dev/null \
    | tr -d '\r' | awk -F': ' 'tolower($1)=="access-control-allow-origin"{print $2}')"
if [ "$ACAO" = "$ORIGIN" ] || [ "$ACAO" = "*" ]; then
    pass "preflight allows $ORIGIN"
else
    fail "preflight for $ORIGIN returned Access-Control-Allow-Origin='${ACAO:-<none>}'"
fi

if [ -n "$PUBLIC_HOST" ]; then
    say "public surface allowlist for $PUBLIC_HOST"
    # The single most consequential nginx assertion: one over-broad proxy_pass turns an
    # auth-free operator API into an internet-facing one.
    PRIV_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
        -H "Host: $PUBLIC_HOST" "http://127.0.0.1/api/private/products" || echo 000)"
    if [ "$PRIV_CODE" = "404" ]; then
        pass "/api/private/* returns 404 through the public vhost"
    else
        fail "/api/private/* returned $PRIV_CODE through the public vhost — expected 404"
    fi

    # Reachability, asserted unambiguously. A status code alone cannot prove this: nginx returns
    # 404 when it blocks the path, and the app *also* returns 404 for an unknown product slug, so
    # "HTTP 404" is consistent with both success and total failure. Discriminate on the body
    # instead — FastAPI answers with JSON (`{"detail": ...}`), nginx with its own HTML error page.
    FUNNEL_BODY="$(curl -sS --max-time 5 \
        -X POST -H "Host: $PUBLIC_HOST" -H 'Content-Type: application/json' -d '{}' \
        "http://127.0.0.1/api/funnel/__verify__/visit" 2>/dev/null || echo '')"
    if printf '%s' "$FUNNEL_BODY" | grep -q '"detail"'; then
        pass "/api/funnel/* reaches the app (JSON error body, not an nginx page)"
    else
        fail "/api/funnel/* did not reach the app — got: $(printf '%s' "${FUNNEL_BODY:-<empty>}" | head -c 80)"
    fi

    # The private path must be the *opposite*: nginx's own page, never the app's JSON. Checking
    # only the status would let a future `proxy_pass /api/` regression pass silently if the app
    # happened to 404 too.
    PRIV_BODY="$(curl -sS --max-time 5 -H "Host: $PUBLIC_HOST" \
        "http://127.0.0.1/api/private/products" 2>/dev/null || echo '')"
    if printf '%s' "$PRIV_BODY" | grep -q '"detail"'; then
        fail "/api/private/* reached the APP through the public vhost — the allowlist is broken"
    else
        pass "/api/private/* is answered by nginx, never proxied"
    fi
else
    say "public surface allowlist"
    echo "  SKIPPED — pass a public hostname to check it:  verify-deploy.sh acme.example"
fi

echo
if [ "$FAILED" -eq 0 ]; then
    echo "All deploy assertions passed."
else
    echo "One or more deploy assertions FAILED — see above." >&2
fi
exit "$FAILED"
