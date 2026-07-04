"""Proactive OAuth token-refresh policy (TECH_SPEC §7/§9, story S4.8).

A channel's access token is only *used* at publish time, so the publish pass decides — right before
use, once the token is within `REFRESH_BUFFER` of expiry — whether it must be refreshed. Two cases:

- **Self-managed credential** (a structured JSON blob, e.g. Reddit's PRAW kwargs): the provider's
  own client refreshes the access token under the hood. We hold no short-lived token of ours, so we
  skip and let publish proceed (see `is_self_managed_credential` + `publish._refresh_if_needed`). A
  dead self-managed refresh token surfaces at publish time as an `AuthFailure`, which fences the
  channel there.
- **Owned credential** (a bare access token we hold — the shape the `/connect` endpoint writes):
  `refresh_channel_token` runs a standard OAuth2 `refresh_token` grant against the provider's token
  endpoint using the refresh token + client credentials seeded in the vault, and writes the new bare
  access token (with its fresh expiry) back — preserving the stored shape. Any failure raises so the
  caller fails the channel safe (`connect_state=failed` + alert).

The network boundary (`_post_token_refresh`) is module-level so tests inject it (matching the
`_build_reddit` seam); the pure parts (`needs_refresh`, `parse_token_response`) are unit-tested. A
provider is refreshable once it's in the `OWNED_TOKEN_PROVIDERS` registry (see `token_endpoint`).

S4.8.2 adds the *acquisition* half — the `OWNED_TOKEN_PROVIDERS` registry (single source of truth
for a provider's endpoints; `token_endpoint` reads it so refresh can't drift from it), an
`authorization_code` exchange behind the same injectable seam, and a signed expiring `state` —
consumed by the authorize/callback endpoints in `api/private/channels.py`.

ponytail: stdlib `urllib` (no new HTTP dependency); `state` reuses the vault Fernet key (no new
table/session store). Live X/Instagram/YouTube provider entries remain out of scope (TECH_SPEC §7).
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.fernet import InvalidToken
from sqlmodel import Session

from app.models import Channel, ChannelType, Product
from app.secrets import vault
from app.secrets.vault import get_credential, put_credential

# Refresh once the token is within this window of expiry (or already past it).
REFRESH_BUFFER = timedelta(minutes=5)

# OAuth `state` lifetime: the redirect round-trip (consent screen) must complete within this window.
# Fernet embeds the mint time, so verify enforces it as a TTL — a stale/replayed state is rejected.
STATE_TTL = timedelta(minutes=10)


class RefreshUnavailable(RuntimeError):
    """No refresh handler is configured for this provider (e.g. its token endpoint isn't
    registered). Distinct from a refresh *failure*: we simply can't proactively refresh, so the
    caller proceeds and lets the reactive `AuthFailure` fence catch an actually-dead token — rather
    than fencing a channel whose token may still be valid."""


@dataclass(frozen=True)
class OAuthProvider:
    """Per-provider OAuth endpoints + default scopes for an owned-token channel (we hold and refresh
    its bare access token). `authorize_url`/`token_url` drive the redirect + code-exchange dance;
    `scopes` are requested at authorize time."""

    authorize_url: str
    token_url: str
    scopes: tuple[str, ...] = ()
    # Extra provider-specific authorize-time query params (tuple-of-pairs: the dataclass is
    # frozen/hashable). e.g. Google needs access_type=offline&prompt=consent or it never returns
    # a refresh token. Core params (state, client_id, …) always win over an entry here.
    authorize_params: tuple[tuple[str, str], ...] = ()


# Owned-token OAuth providers, keyed by ChannelType. Add an entry per provider as it goes live; that
# entry is the single registration point (it also populates `TOKEN_ENDPOINTS` below, so proactive
# refresh starts working for it). Blog has no OAuth and Reddit self-manages via PRAW, so neither is
# listed. Full X/Instagram/YouTube live integrations remain out of scope (TECH_SPEC §7) — the
# machinery is verified end-to-end in tests via an injected provider (`monkeypatch.setitem`).
OWNED_TOKEN_PROVIDERS: dict[ChannelType, OAuthProvider] = {
    # S5.1: YouTube (Google OAuth) is the first live owned-token provider — we hold and refresh its
    # bare access token. Google returns a refresh token only when the authorize URL carries
    # `access_type=offline&prompt=consent`, so they ride the registry entry.
    ChannelType.YOUTUBE: OAuthProvider(
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=(
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
        ),
        authorize_params=(("access_type", "offline"), ("prompt", "consent")),
    ),
}

# Optional per-type token-endpoint overrides. The registry above is the single source of truth for a
# provider's token URL (see `token_endpoint`); this dict only exists for edge providers registered
# outside the registry or for test injection. Empty in v1. Self-managed providers (Reddit/PRAW) are
# never listed — their client refreshes itself.
TOKEN_ENDPOINTS: dict[ChannelType, str] = {}


def token_endpoint(channel_type: ChannelType) -> str | None:
    """The OAuth2 token endpoint to refresh this channel type, or None if it has no owned-token
    provider. An explicit `TOKEN_ENDPOINTS` override wins; otherwise it comes from the registry, so
    registering a provider makes it refreshable (S4.8) with no second edit and no drift."""
    if channel_type in TOKEN_ENDPOINTS:
        return TOKEN_ENDPOINTS[channel_type]
    provider = OWNED_TOKEN_PROVIDERS.get(channel_type)
    return provider.token_url if provider else None


def needs_refresh(expires_at: datetime | None, now: datetime) -> bool:
    """True when a stored token is within `REFRESH_BUFFER` of expiry (or expired). With no known
    expiry there is nothing to proactively refresh, so return False."""
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:  # SQLite hands datetimes back tz-naive
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at - now <= REFRESH_BUFFER


def is_self_managed_credential(value: str) -> bool:
    """True if the credential is a structured JSON blob (e.g. Reddit's PRAW kwargs) rather than a
    bare access-token string. Such credentials carry a refresh token consumed by the provider's own
    client, which refreshes access tokens under the hood — so we must NOT proactively refresh them
    (there is no short-lived token of ours to replace, and writing a bare token would corrupt the
    shape the adapter parses). A bare-token credential, by contrast, is ours to refresh."""
    try:
        return isinstance(json.loads(value), dict)
    except (ValueError, TypeError):
        return False


def parse_token_response(data: dict, now: datetime) -> tuple[str, datetime | None]:
    """Extract (access_token, expires_at) from an OAuth2 token response. `expires_in` is optional;
    a response without an access token is a refresh failure."""
    token = data.get("access_token")
    if not token:
        raise RuntimeError("token refresh response has no access_token")
    expires_in = data.get("expires_in")
    # `is not None` (not truthiness): expires_in=0 means "already expired", not "unknown" — dropping
    # it to None would make the next publish skip refresh for a dead token.
    expires_at = now + timedelta(seconds=int(expires_in)) if expires_in is not None else None
    return token, expires_at


def _post_token_refresh(
    endpoint: str, refresh_token: str, client_id: str, client_secret: str
) -> dict:  # pragma: no cover - real network; injected in tests, exercised against the provider
    """OAuth2 refresh_token grant with HTTP Basic client auth. Raises on any HTTP/parse error.

    The `Authorization` header is sent up front (not via `HTTPBasicAuthHandler`, which only
    responds to a 401 challenge) — token endpoints that require client auth on the initial POST
    (e.g. Reddit) reject a challenge-less first request."""
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}
    ).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(  # noqa: S310 - fixed https endpoint from TOKEN_ENDPOINTS
        endpoint, data=body, method="POST", headers={"Authorization": f"Basic {basic}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https endpoint
        return json.loads(resp.read().decode())


def refresh_channel_token(
    session: Session, product: Product, channel: Channel, now: datetime
) -> None:
    """Refresh a *bare access-token* `{type}_oauth` credential in the vault via a standard OAuth2
    refresh_token grant. Raises on any failure so the caller fails the channel safe. The token
    endpoint comes from `TOKEN_ENDPOINTS`; client credentials from the vault
    (`{type}_client_id`/`{type}_client_secret`). Self-managed structured blobs are filtered out
    upstream, so this only ever sees bare tokens."""
    prefix = channel.type.value
    endpoint = token_endpoint(channel.type)
    if endpoint is None:
        raise RefreshUnavailable(
            f"no OAuth token endpoint registered for channel type {channel.type}"
        )
    refresh_token = get_credential(
        session, product.id, f"{prefix}_oauth_refresh", channel_id=channel.id
    )
    client_id = get_credential(session, product.id, f"{prefix}_client_id", channel_id=channel.id)
    client_secret = get_credential(
        session, product.id, f"{prefix}_client_secret", channel_id=channel.id
    )
    if not (refresh_token and client_id and client_secret):
        raise RuntimeError(
            f"channel {channel.id} is missing OAuth refresh credentials "
            f"({prefix}_oauth_refresh / {prefix}_client_id / {prefix}_client_secret)"
        )
    data = _post_token_refresh(endpoint, refresh_token, client_id, client_secret)
    access_token, expires_at = parse_token_response(data, now)
    put_credential(
        session,
        product.id,
        f"{prefix}_oauth",
        access_token,
        channel_id=channel.id,
        expires_at=expires_at,
    )
    # Refresh-token rotation: providers that return a new refresh token revoke the old one, so
    # persist it — otherwise the next refresh would present a dead token and needlessly fence.
    rotated = data.get("refresh_token")
    if rotated and rotated != refresh_token:
        put_credential(
            session, product.id, f"{prefix}_oauth_refresh", rotated, channel_id=channel.id
        )


# --- authorize → callback (S4.8.2) -------------------------------------------
#
# The refresh grant above tops up a token we already hold; this section is the *initial*
# acquisition: an authorize redirect, then a one-time `authorization_code` exchange on callback.


def build_authorize_url(
    provider: OAuthProvider, client_id: str, redirect_uri: str, state: str
) -> str:
    """Provider consent URL for the redirect leg. Scopes are space-joined per RFC 6749; `state` is
    the signed anti-CSRF token minted below. Provider extras (`authorize_params`, e.g. Google's
    access_type=offline) merge first so the core protocol params can never be overridden."""
    query = urllib.parse.urlencode(
        {
            **dict(provider.authorize_params),
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(provider.scopes),
            "state": state,
        }
    )
    return f"{provider.authorize_url}?{query}"


def _post_token_exchange(
    endpoint: str, code: str, redirect_uri: str, client_id: str, client_secret: str
) -> dict:  # pragma: no cover - real network; injected in tests, exercised against the provider
    """OAuth2 authorization_code grant with HTTP Basic client auth — the acquisition counterpart to
    `_post_token_refresh`. Same seam shape so tests monkeypatch it (no-mocking house rule)."""
    body = urllib.parse.urlencode(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
    ).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(  # noqa: S310 - fixed https endpoint from OWNED_TOKEN_PROVIDERS
        endpoint, data=body, method="POST", headers={"Authorization": f"Basic {basic}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https endpoint
        return json.loads(resp.read().decode())


def exchange_authorization_code(
    provider: OAuthProvider,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    now: datetime,
) -> tuple[str, str | None, datetime | None]:
    """Complete OAuth: swap a callback `code` for (access_token, refresh_token?, expires_at?).
    Reuses `parse_token_response` for the access token + expiry; the refresh token (if returned) is
    what later `refresh_channel_token` calls present."""
    data = _post_token_exchange(provider.token_url, code, redirect_uri, client_id, client_secret)
    access_token, expires_at = parse_token_response(data, now)
    return access_token, data.get("refresh_token"), expires_at


# --- signed, expiring OAuth `state` ------------------------------------------
#
# CSRF protection for the redirect round-trip with no new DB table or session store: Fernet-encrypt
# the (product, channel) the flow is for under the existing vault key, and verify it (with TTL) on
# callback. The vault key is reused directly (not via vault.encrypt) so state tokens are NOT added
# to the log-redaction set — they carry no secret, only ids already present in the request path.


class InvalidState(ValueError):
    """The callback `state` is tampered, forged, expired, or for a different (product, channel)."""


def mint_state(product_id: int, channel_id: int) -> str:
    """Sign a `state` binding this OAuth flow to (product_id, channel_id)."""
    payload = json.dumps({"p": product_id, "c": channel_id}).encode()
    return vault._fernet().encrypt(payload).decode()


def verify_state(state: str, product_id: int, channel_id: int) -> None:
    """Reject a forged `state`, one older than `STATE_TTL`, or one for a different channel."""
    try:
        payload = vault._fernet().decrypt(state.encode(), ttl=int(STATE_TTL.total_seconds()))
        data = json.loads(payload)
    except (InvalidToken, ValueError) as exc:
        raise InvalidState("invalid or expired OAuth state") from exc
    if data.get("p") != product_id or data.get("c") != channel_id:
        raise InvalidState("OAuth state does not match this channel")
