"""S0.3: CORS origins accept comma-separated env input (not JSON).
S4.8.2: the OAuth redirect base must fail closed on non-https off localhost."""

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
