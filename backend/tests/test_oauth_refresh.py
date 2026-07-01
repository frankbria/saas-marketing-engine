"""S4.8: pure helpers for the proactive OAuth-refresh policy (no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.modules.crank.oauth_refresh import (
    REFRESH_BUFFER,
    is_self_managed_credential,
    needs_refresh,
)

NOW = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)


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


def test_parse_token_response_missing_token_raises():
    import pytest

    from app.modules.crank.oauth_refresh import parse_token_response

    with pytest.raises(RuntimeError, match="no access_token"):
        parse_token_response({"error": "invalid_grant"}, NOW)
