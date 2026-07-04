"""S0.3: CORS origins accept comma-separated env input (not JSON).
S4.8.2: the OAuth redirect base must fail closed on non-https off localhost.
S6.2: heartbeat digest/alert settings — sane defaults, bounded so bad values fail at startup.
S5.0: Celery broker + ephemeral GPU provisioner settings — bounded, secret-safe, and the
spend cap must fail loudly when it cannot be enforced (cap set but rate unknown)."""

import pytest
from pydantic import SecretStr

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


def test_media_gpu_defaults(monkeypatch):
    # Local .env may set the real key; the defaults under test are everything else.
    monkeypatch.delenv("SME_GPU_API_KEY", raising=False)
    s = Settings(_env_file=None)
    assert s.celery_broker_url == "redis://localhost:6379/0"
    assert s.gpu_provider == "runpod"
    assert s.gpu_api_key is None
    assert s.gpu_pod_template_id is None
    assert s.gpu_idle_teardown_minutes == 10
    assert s.media_gpu_monthly_cap_cents == 0  # 0 = unlimited, matching token_budget convention
    assert s.gpu_pod_rate_cents_per_minute == 2
    assert s.media_provisioner_interval_seconds == 60


def test_gpu_api_key_is_secret(monkeypatch):
    monkeypatch.setenv("SME_GPU_API_KEY", "test-key-placeholder")
    s = Settings()
    assert isinstance(s.gpu_api_key, SecretStr)
    assert "test-key-placeholder" not in repr(s)
    assert s.gpu_api_key.get_secret_value() == "test-key-placeholder"


@pytest.mark.parametrize(
    ("env", "value"),
    [
        ("SME_GPU_IDLE_TEARDOWN_MINUTES", "0"),  # 0 would thrash boot/teardown every tick
        ("SME_MEDIA_GPU_MONTHLY_CAP_CENTS", "-1"),
        ("SME_GPU_POD_RATE_CENTS_PER_MINUTE", "-1"),
        ("SME_MEDIA_PROVISIONER_INTERVAL_SECONDS", "0"),
    ],
)
def test_media_gpu_settings_bounded(monkeypatch, env, value):
    # An out-of-range deploy value must fail at startup, not misbehave silently at runtime.
    monkeypatch.setenv(env, value)
    with pytest.raises(ValueError):
        Settings()


def test_cap_without_rate_fails_at_startup(monkeypatch):
    # A cap with a zero per-minute rate could never trip — the guardrail would silently
    # not exist. Fail loud at startup instead (same philosophy as the critic bounds).
    monkeypatch.setenv("SME_MEDIA_GPU_MONTHLY_CAP_CENTS", "5000")
    monkeypatch.setenv("SME_GPU_POD_RATE_CENTS_PER_MINUTE", "0")
    with pytest.raises(ValueError, match="rate"):
        Settings()


def test_unknown_gpu_provider_fails_at_startup(monkeypatch):
    # Only the implemented provider is legal config — a typo'd deploy value must fail
    # loudly at boot, not at first provisioning attempt hours later.
    monkeypatch.setenv("SME_GPU_PROVIDER", "aws")
    with pytest.raises(ValueError, match="gpu_provider"):
        Settings()


def test_video_pipeline_defaults():
    # S5.1: ElevenLabs TTS key is a secret (None until set — TTS then fails loudly), and the
    # render transfer guard / tick cadence / re-dispatch bound carry sane bounded defaults.
    s = Settings()
    assert s.elevenlabs_api_key is None
    assert s.elevenlabs_voice_id  # non-empty default voice
    assert s.video_render_max_bytes > 0
    assert s.video_render_tick_seconds >= 5
    assert s.video_max_render_dispatches >= 1


def test_elevenlabs_api_key_is_secret(monkeypatch):
    # §9: provider keys must never leak via Settings repr/model_dump.
    monkeypatch.setenv("SME_ELEVENLABS_API_KEY", "el-secret-123")
    s = Settings()
    assert isinstance(s.elevenlabs_api_key, SecretStr)
    assert "el-secret-123" not in repr(s)
    assert s.elevenlabs_api_key.get_secret_value() == "el-secret-123"


@pytest.mark.parametrize(
    ("env", "value"),
    [
        ("SME_VIDEO_RENDER_MAX_BYTES", "0"),  # 0 would reject every render, silently killing video
        ("SME_VIDEO_RENDER_TICK_SECONDS", "0"),
        ("SME_VIDEO_MAX_RENDER_DISPATCHES", "0"),  # 0 would strand every item in `rendering`
    ],
)
def test_video_pipeline_settings_bounded(monkeypatch, env, value):
    # An out-of-range deploy value must fail at startup, not misbehave silently at runtime.
    monkeypatch.setenv(env, value)
    with pytest.raises(ValueError):
        Settings()
