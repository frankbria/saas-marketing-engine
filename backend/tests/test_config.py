"""S0.3: CORS origins accept comma-separated env input (not JSON)."""

from app.config import Settings


def test_cors_origins_default():
    assert Settings().cors_origins == ["http://localhost:3010"]


def test_cors_origins_csv(monkeypatch):
    monkeypatch.setenv("SME_CORS_ORIGINS", "http://a.test, http://b.test")
    assert Settings().cors_origins == ["http://a.test", "http://b.test"]
