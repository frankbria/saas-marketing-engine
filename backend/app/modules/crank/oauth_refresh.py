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
provider is refreshable once its token endpoint is registered in `TOKEN_ENDPOINTS`.

ponytail: stdlib `urllib` (no new HTTP dependency). Full authorize→callback OAuth redirect + the
provider-registration UI remain deferred (as `api/private/channels.py` already notes).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from app.models import Channel, ChannelType, Product
from app.secrets.vault import get_credential, put_credential

# Refresh once the token is within this window of expiry (or already past it).
REFRESH_BUFFER = timedelta(minutes=5)


class RefreshUnavailable(RuntimeError):
    """No refresh handler is configured for this provider (e.g. its token endpoint isn't
    registered). Distinct from a refresh *failure*: we simply can't proactively refresh, so the
    caller proceeds and lets the reactive `AuthFailure` fence catch an actually-dead token — rather
    than fencing a channel whose token may still be valid."""


# OAuth2 token endpoints for providers whose bare access token we hold and refresh ourselves.
# Self-managed providers (e.g. Reddit via PRAW) are NOT listed — their client refreshes internally.
TOKEN_ENDPOINTS: dict[ChannelType, str] = {}


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
    expires_at = now + timedelta(seconds=int(expires_in)) if expires_in else None
    return token, expires_at


def _post_token_refresh(
    endpoint: str, refresh_token: str, client_id: str, client_secret: str
) -> dict:  # pragma: no cover - real network; injected in tests, exercised against the provider
    """OAuth2 refresh_token grant with HTTP Basic client auth. Raises on any HTTP/parse error."""
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}
    ).encode()
    req = urllib.request.Request(endpoint, data=body, method="POST")  # noqa: S310 - fixed https
    creds = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    creds.add_password(None, endpoint, client_id, client_secret)
    opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(creds))
    with opener.open(req, timeout=30) as resp:
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
    endpoint = TOKEN_ENDPOINTS.get(channel.type)
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
