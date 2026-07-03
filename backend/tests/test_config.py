"""S0.3: CORS origins accept comma-separated env input (not JSON).
S4.8.2: the OAuth redirect base must fail closed on non-https off localhost.
S6.2: heartbeat digest/alert settings — sane defaults, bounded so bad values fail at startup."""

import pytest

from app.config import Settings


def test_cors_origins_default():
    assert Settings().cors_origins == ["http://localhost:3010"]


def test_cors_origins_csv(monkeypatch):
    monkeypatch.setenv("SME_CORS_ORIGINS", "http://a.test, http://b.test")
    assert Settings().cors_origins == ["http://a.test", "http://b.test"]


def test_oauth_redirect_allows_http_on_loopback(monkeypatch):
    for url in ("http://localhost:8010", "http://127.0.0.1:8010", "https://app.example.com"):
        monkeypatch.setenv("SME_OAUTH_REDIRECT_BASE_URL", url)
        assert Settings().oauth_redirect_base_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://app.example.com",
        "HTTP://APP.EXAMPLE.COM",
        "ftp://app.example.com",
        "app.example.com",
    ],
)
def test_oauth_redirect_rejects_non_https_off_localhost(monkeypatch, url):
    # Requiring https (not just rejecting the literal http:// prefix) closes uppercase / other-
    # scheme / scheme-less bypasses — OAuth code/state must never cross the wire in plaintext.
    monkeypatch.setenv("SME_OAUTH_REDIRECT_BASE_URL", url)
    with pytest.raises(ValueError, match="https off localhost"):
        Settings()


def test_heartbeat_defaults():
    s = Settings()
    assert s.heartbeat_digest_hour_utc == 6
    assert s.heartbeat_publish_fail_threshold == 2
    assert s.heartbeat_zero_reach_window_days == 7
    assert s.alert_email_to is None  # delivery stays log-only until configured


@pytest.mark.parametrize(
    ("env", "value"),
    [
        ("SME_HEARTBEAT_DIGEST_HOUR_UTC", "24"),
        ("SME_HEARTBEAT_PUBLISH_FAIL_THRESHOLD", "0"),
        ("SME_HEARTBEAT_ZERO_REACH_WINDOW_DAYS", "0"),
    ],
)
def test_heartbeat_settings_bounded(monkeypatch, env, value):
    # An out-of-range deploy value must fail at startup, not misfire silently at runtime.
    monkeypatch.setenv(env, value)
    with pytest.raises(ValueError):
        Settings()
