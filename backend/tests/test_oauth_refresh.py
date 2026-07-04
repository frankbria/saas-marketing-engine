"""S4.8/S4.8.2: pure helpers for the OAuth-refresh policy and the authorize→callback machinery
(no network — the exchange seam is injected; `state` uses the real vault key via a temp fixture)."""

from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime, timedelta

import pytest

from app.models import ChannelType
from app.modules.crank import oauth_refresh
from app.modules.crank.oauth_refresh import (
    OWNED_TOKEN_PROVIDERS,
    REFRESH_BUFFER,
    TOKEN_ENDPOINTS,
    InvalidState,
    OAuthProvider,
    build_authorize_url,
    exchange_authorization_code,
    is_self_managed_credential,
    mint_state,
    needs_refresh,
    verify_state,
)
from app.secrets import vault

NOW = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)

_PROVIDER = OAuthProvider(
    authorize_url="https://provider.test/authorize",
    token_url="https://provider.test/token",
    scopes=("read", "write"),
)


@pytest.fixture
def _vault_key(monkeypatch):
    # `state` signing/verification uses the vault Fernet key — give it a real one to round-trip.
    monkeypatch.setattr(vault.settings, "vault_key", vault.generate_key())


def test_needs_refresh_none_expiry_is_false():
    assert needs_refresh(None, NOW) is False


def test_needs_refresh_within_buffer_and_past():
    assert needs_refresh(NOW + REFRESH_BUFFER - timedelta(seconds=1), NOW) is True
    assert needs_refresh(NOW - timedelta(hours=1), NOW) is True  # already expired


def test_needs_refresh_beyond_buffer_is_false():
    assert needs_refresh(NOW + REFRESH_BUFFER + timedelta(minutes=1), NOW) is False


def test_needs_refresh_normalizes_naive_expiry():
    # SQLite returns tz-naive datetimes; comparison must not raise.
    assert needs_refresh((NOW - timedelta(minutes=1)).replace(tzinfo=None), NOW) is True


def test_is_self_managed_credential_distinguishes_shapes():
    assert is_self_managed_credential('{"client_id": "x", "refresh_token": "z"}') is True
    assert is_self_managed_credential("bare-access-token") is False


def test_parse_token_response_with_expiry():
    from app.modules.crank.oauth_refresh import parse_token_response

    token, expires_at = parse_token_response({"access_token": "abc", "expires_in": 3600}, NOW)
    assert token == "abc"
    assert expires_at == NOW + timedelta(seconds=3600)


def test_parse_token_response_without_expiry():
    from app.modules.crank.oauth_refresh import parse_token_response

    token, expires_at = parse_token_response({"access_token": "abc"}, NOW)
    assert token == "abc"
    assert expires_at is None


def test_parse_token_response_zero_expiry_is_immediate():
    # expires_in=0 means already expired — must NOT be dropped to "unknown" (None).
    from app.modules.crank.oauth_refresh import parse_token_response

    token, expires_at = parse_token_response({"access_token": "abc", "expires_in": 0}, NOW)
    assert token == "abc"
    assert expires_at == NOW


def test_parse_token_response_missing_token_raises():
    from app.modules.crank.oauth_refresh import parse_token_response

    with pytest.raises(RuntimeError, match="no access_token"):
        parse_token_response({"error": "invalid_grant"}, NOW)


# ---- S4.8.2: provider registry + authorize URL ------------------------------------------


def test_token_endpoint_reads_registry_as_single_source(monkeypatch):
    # A registered provider is refreshable straight from the registry — no separate TOKEN_ENDPOINTS
    # edit, so the two can't drift. v1 ships neither dict populated (X/IG/YT out of scope).
    assert TOKEN_ENDPOINTS == {}
    assert oauth_refresh.token_endpoint(ChannelType.X) is None
    monkeypatch.setitem(OWNED_TOKEN_PROVIDERS, ChannelType.X, _PROVIDER)
    assert oauth_refresh.token_endpoint(ChannelType.X) == _PROVIDER.token_url


def test_token_endpoint_override_wins(monkeypatch):
    # An explicit TOKEN_ENDPOINTS entry (edge provider / test injection) takes precedence.
    monkeypatch.setitem(OWNED_TOKEN_PROVIDERS, ChannelType.X, _PROVIDER)
    monkeypatch.setitem(TOKEN_ENDPOINTS, ChannelType.X, "https://override.test/token")
    assert oauth_refresh.token_endpoint(ChannelType.X) == "https://override.test/token"


def test_build_authorize_url_has_scopes_state_and_redirect():
    url = build_authorize_url(_PROVIDER, "client-abc", "https://us/cb", "sig-state")
    base, _, query = url.partition("?")
    assert base == "https://provider.test/authorize"
    q = urllib.parse.parse_qs(query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["client-abc"]
    assert q["redirect_uri"] == ["https://us/cb"]
    assert q["scope"] == ["read write"]  # space-joined per RFC 6749
    assert q["state"] == ["sig-state"]


def test_build_authorize_url_carries_provider_extra_params():
    # S5.1: Google only returns a refresh token when the authorize URL carries
    # access_type=offline&prompt=consent — without them the first refresh would fence the
    # channel. Providers declare such extras on the registry entry; core params must win
    # over a (misconfigured) extra of the same name.
    provider = OAuthProvider(
        authorize_url="https://provider.test/authorize",
        token_url="https://provider.test/token",
        scopes=("read",),
        authorize_params=(("access_type", "offline"), ("prompt", "consent"), ("state", "evil")),
    )
    url = build_authorize_url(provider, "client-abc", "https://us/cb", "sig-state")
    q = urllib.parse.parse_qs(url.partition("?")[2])
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    assert q["state"] == ["sig-state"]  # the signed anti-CSRF state is not overridable


def test_youtube_provider_requests_offline_access():
    # The live YouTube registration must actually carry the refresh-token extras — the
    # generic test above only proves the mechanism.
    provider = OWNED_TOKEN_PROVIDERS[ChannelType.YOUTUBE]
    assert ("access_type", "offline") in provider.authorize_params
    assert ("prompt", "consent") in provider.authorize_params


# ---- S4.8.2: authorization_code exchange (injected seam) --------------------------------


def test_exchange_authorization_code_returns_tokens_and_expiry(monkeypatch):
    captured = {}

    def fake_post(endpoint, code, redirect_uri, client_id, client_secret):
        captured.update(
            endpoint=endpoint, code=code, redirect=redirect_uri, cid=client_id, csec=client_secret
        )
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

    monkeypatch.setattr(oauth_refresh, "_post_token_exchange", fake_post)

    access, refresh, expires_at = exchange_authorization_code(
        _PROVIDER, "the-code", "https://us/cb", "cid", "csec", NOW
    )
    assert (access, refresh) == ("at", "rt")
    assert expires_at == NOW + timedelta(seconds=3600)
    assert captured == {
        "endpoint": "https://provider.test/token",
        "code": "the-code",
        "redirect": "https://us/cb",
        "cid": "cid",
        "csec": "csec",
    }


def test_exchange_authorization_code_without_refresh_token(monkeypatch):
    # A provider that returns no refresh_token yields None (callback then stores no refresh cred).
    monkeypatch.setattr(
        oauth_refresh, "_post_token_exchange", lambda *a: {"access_token": "at", "expires_in": 60}
    )
    access, refresh, expires_at = exchange_authorization_code(
        _PROVIDER, "c", "https://us/cb", "cid", "csec", NOW
    )
    assert access == "at" and refresh is None
    assert expires_at == NOW + timedelta(seconds=60)


# ---- S4.8.2: signed, expiring OAuth state ----------------------------------------------


def test_state_round_trips_for_matching_channel(_vault_key):
    verify_state(mint_state(7, 3), 7, 3)  # does not raise


def test_state_rejected_for_wrong_channel(_vault_key):
    state = mint_state(7, 3)
    with pytest.raises(InvalidState):
        verify_state(state, 7, 4)
    with pytest.raises(InvalidState):
        verify_state(state, 8, 3)


def test_state_rejected_when_tampered(_vault_key):
    state = mint_state(7, 3)
    with pytest.raises(InvalidState):
        verify_state(state[:-2] + "xx", 7, 3)


def test_state_rejected_when_expired(_vault_key):
    # Fernet embeds the mint time; a token minted long ago (epoch 0) is past STATE_TTL → rejected.
    import json

    payload = json.dumps({"p": 7, "c": 3}).encode()
    stale = vault._fernet().encrypt_at_time(payload, 0).decode()
    with pytest.raises(InvalidState):
        verify_state(stale, 7, 3)
