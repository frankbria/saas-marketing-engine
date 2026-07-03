"""S6.2: heartbeat digest + alerts (TECH_SPEC §8.4, PRD FR-31) — real DB, no mocking."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import get_session
from app.main import create_app
from app.models import (
    Channel,
    ChannelType,
    ConnectState,
    ContentItem,
    ContentItemStatus,
    HeartbeatDigest,
    MetricEvent,
    MetricStage,
    Product,
)
from app.modules.heartbeat import build_digest, evaluate_alerts, run_heartbeat

NOW = datetime(2026, 7, 2, 6, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "test.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def client(engine):
    def _session_override():
        with Session(engine) as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _session_override
    with TestClient(app) as c:
        yield c


def _make_product(engine, *, slug="acme") -> Product:
    with Session(engine) as s:
        p = Product(name=slug, slug=slug, marketing_domain=f"{slug}.example")
        s.add(p)
        s.commit()
        s.refresh(p)
        return p


def _make_channel(engine, product_id: int, ctype=ChannelType.BLOG, **kwargs) -> Channel:
    with Session(engine) as s:
        ch = Channel(product_id=product_id, type=ctype, enabled=True, autonomous=True, **kwargs)
        s.add(ch)
        s.commit()
        s.refresh(ch)
        return ch


def _add_item(engine, product_id: int, channel_id: int, *, status, published_at=None, error=None):
    with Session(engine) as s:
        item = ContentItem(
            product_id=product_id,
            channel_id=channel_id,
            content_type="blog",
            body="body",
            status=status,
            published_at=published_at,
            error=error,
        )
        s.add(item)
        s.commit()
        s.refresh(item)
        return item


def _add_impressions(engine, product_id: int, channel_id: int, count: int, occurred_at: datetime):
    with Session(engine) as s:
        for _ in range(count):
            s.add(
                MetricEvent(
                    product_id=product_id,
                    channel_id=channel_id,
                    stage=MetricStage.IMPRESSION,
                    value=1,
                    occurred_at=occurred_at,
                )
            )
        s.commit()


# --- build_digest ------------------------------------------------------------


def test_digest_counts_published_failed_reach_per_channel(engine):
    p = _make_product(engine)
    blog = _make_channel(engine, p.id, ChannelType.BLOG)
    reddit = _make_channel(engine, p.id, ChannelType.REDDIT)

    # blog: 2 published in window, 1 published outside window, 3 impressions in window
    _add_item(
        engine,
        p.id,
        blog.id,
        status=ContentItemStatus.PUBLISHED,
        published_at=NOW - timedelta(hours=2),
    )
    _add_item(
        engine,
        p.id,
        blog.id,
        status=ContentItemStatus.PUBLISHED,
        published_at=NOW - timedelta(hours=23),
    )
    _add_item(
        engine,
        p.id,
        blog.id,
        status=ContentItemStatus.PUBLISHED,
        published_at=NOW - timedelta(days=2),
    )
    _add_impressions(engine, p.id, blog.id, 3, NOW - timedelta(hours=1))
    _add_impressions(engine, p.id, blog.id, 5, NOW - timedelta(days=3))  # outside window

    # reddit: 1 stuck publish_failed (stock, not flow)
    _add_item(engine, p.id, reddit.id, status=ContentItemStatus.PUBLISH_FAILED, error="boom")

    with Session(engine) as s:
        digest = build_digest(s, p, NOW)

    rows = {r["channel_type"]: r for r in digest["channels"]}
    assert rows["blog"]["published"] == 2
    assert rows["blog"]["failed"] == 0
    assert rows["blog"]["reach"] == 3
    assert rows["reddit"]["published"] == 0
    assert rows["reddit"]["failed"] == 1
    assert rows["reddit"]["reach"] == 0


def test_digest_ignores_other_products(engine):
    p = _make_product(engine)
    other = _make_product(engine, slug="other")
    _make_channel(engine, p.id, ChannelType.BLOG)
    other_ch = _make_channel(engine, other.id, ChannelType.BLOG)
    _add_item(
        engine,
        other.id,
        other_ch.id,
        status=ContentItemStatus.PUBLISHED,
        published_at=NOW - timedelta(hours=1),
    )

    with Session(engine) as s:
        digest = build_digest(s, p, NOW)

    assert digest["channels"][0]["published"] == 0


# --- evaluate_alerts ---------------------------------------------------------


def test_alert_repeated_publish_fail_at_threshold(engine):
    p = _make_product(engine)
    ch = _make_channel(engine, p.id, ChannelType.REDDIT)
    _add_item(engine, p.id, ch.id, status=ContentItemStatus.PUBLISH_FAILED)
    _add_item(engine, p.id, ch.id, status=ContentItemStatus.PUBLISH_FAILED)

    with Session(engine) as s:
        digest = build_digest(s, p, NOW)
        alerts = evaluate_alerts(s, p, digest, NOW)

    kinds = [a["kind"] for a in alerts]
    assert "repeated_publish_fail" in kinds


def test_no_publish_fail_alert_below_threshold(engine):
    p = _make_product(engine)
    ch = _make_channel(engine, p.id, ChannelType.REDDIT)
    _add_item(engine, p.id, ch.id, status=ContentItemStatus.PUBLISH_FAILED)

    with Session(engine) as s:
        digest = build_digest(s, p, NOW)
        alerts = evaluate_alerts(s, p, digest, NOW)

    assert [a["kind"] for a in alerts] == []


def test_alert_dead_oauth_token(engine):
    p = _make_product(engine)
    _make_channel(engine, p.id, ChannelType.REDDIT, connect_state=ConnectState.FAILED)

    with Session(engine) as s:
        digest = build_digest(s, p, NOW)
        alerts = evaluate_alerts(s, p, digest, NOW)

    assert [a["kind"] for a in alerts] == ["oauth_token_dead"]


def test_alert_zero_reach_when_published_but_no_impressions(engine):
    p = _make_product(engine)
    ch = _make_channel(engine, p.id, ChannelType.BLOG)
    # published 3 days ago, inside the 7-day zero-reach window; zero impressions ever
    _add_item(
        engine,
        p.id,
        ch.id,
        status=ContentItemStatus.PUBLISHED,
        published_at=NOW - timedelta(days=3),
    )

    with Session(engine) as s:
        digest = build_digest(s, p, NOW)
        alerts = evaluate_alerts(s, p, digest, NOW)

    assert [a["kind"] for a in alerts] == ["zero_reach"]


def test_no_zero_reach_alert_when_channel_has_reach(engine):
    p = _make_product(engine)
    ch = _make_channel(engine, p.id, ChannelType.BLOG)
    _add_item(
        engine,
        p.id,
        ch.id,
        status=ContentItemStatus.PUBLISHED,
        published_at=NOW - timedelta(days=3),
    )
    _add_impressions(engine, p.id, ch.id, 1, NOW - timedelta(days=1))

    with Session(engine) as s:
        digest = build_digest(s, p, NOW)
        alerts = evaluate_alerts(s, p, digest, NOW)

    assert [a["kind"] for a in alerts] == []


def test_no_zero_reach_alert_when_nothing_published(engine):
    """A quiet channel (nothing published in the window) is not a shadowban signal."""
    p = _make_product(engine)
    _make_channel(engine, p.id, ChannelType.BLOG)

    with Session(engine) as s:
        digest = build_digest(s, p, NOW)
        alerts = evaluate_alerts(s, p, digest, NOW)

    assert [a["kind"] for a in alerts] == []


# --- run_heartbeat -----------------------------------------------------------


def test_run_heartbeat_persists_digest_per_product(engine):
    p1 = _make_product(engine)
    p2 = _make_product(engine, slug="beta")
    _make_channel(engine, p1.id, ChannelType.BLOG)
    _make_channel(engine, p2.id, ChannelType.BLOG)

    with Session(engine) as s:
        created = run_heartbeat(s, NOW)

    assert len(created) == 2
    with Session(engine) as s:
        rows = s.exec(select(HeartbeatDigest)).all()
    assert {r.product_id for r in rows} == {p1.id, p2.id}
    for r in rows:
        assert json.loads(r.digest_json)["channels"]
        assert isinstance(json.loads(r.alerts_json), list)


def test_run_heartbeat_idempotent_within_a_day(engine):
    p = _make_product(engine)
    _make_channel(engine, p.id, ChannelType.BLOG)

    with Session(engine) as s:
        first = run_heartbeat(s, NOW)
        second = run_heartbeat(s, NOW + timedelta(hours=3))  # same UTC day

    assert len(first) == 1
    assert second == []
    with Session(engine) as s:
        assert len(s.exec(select(HeartbeatDigest)).all()) == 1


def test_run_heartbeat_new_digest_next_day(engine):
    p = _make_product(engine)
    _make_channel(engine, p.id, ChannelType.BLOG)

    with Session(engine) as s:
        run_heartbeat(s, NOW)
        next_day = run_heartbeat(s, NOW + timedelta(days=1))

    assert len(next_day) == 1
    with Session(engine) as s:
        assert len(s.exec(select(HeartbeatDigest)).all()) == 2


def test_run_heartbeat_fires_alerts_via_choke_point(engine, caplog):
    p = _make_product(engine)
    _make_channel(engine, p.id, ChannelType.REDDIT, connect_state=ConnectState.FAILED)

    with caplog.at_level("WARNING", logger="app.alerts"):
        with Session(engine) as s:
            run_heartbeat(s, NOW)

    assert any("ALERT oauth_token_dead" in r.getMessage() for r in caplog.records)


def test_run_heartbeat_one_product_crash_does_not_block_others(engine, monkeypatch):
    """§8.3: a crashed job never blocks other products."""
    import app.modules.heartbeat as heartbeat_mod

    p1 = _make_product(engine)
    p2 = _make_product(engine, slug="beta")
    _make_channel(engine, p1.id, ChannelType.BLOG)
    _make_channel(engine, p2.id, ChannelType.BLOG)

    real_build = heartbeat_mod.build_digest

    def _boom_on_p1(session, product, now):
        if product.id == p1.id:
            raise RuntimeError("boom")
        return real_build(session, product, now)

    monkeypatch.setattr(heartbeat_mod, "build_digest", _boom_on_p1)

    with Session(engine) as s:
        created = run_heartbeat(s, NOW)

    assert [r.product_id for r in created] == [p2.id]
    with Session(engine) as s:
        rows = s.exec(select(HeartbeatDigest)).all()
    assert {r.product_id for r in rows} == {p2.id}


# --- email delivery (S6.2 wires it into the raise_alert choke point) ----------


class _FakeSMTP:
    """Records messages handed to send_message; no network (same pattern as test_welcome_email)."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def _reset_fake_smtp(monkeypatch):
    _FakeSMTP.instances = []
    # Hermetic vault: earlier test files register real plaintexts (e.g. "a", "tok-abc") in the
    # process-global secrets set via encrypt(); email-body redaction would then rewrite digest
    # bodies and make these assertions order-dependent.
    from app.secrets import vault

    monkeypatch.setattr(vault, "_secrets", set())


def test_raise_alert_stays_log_only_when_unconfigured(monkeypatch):
    from app import config
    from app.integrations import email as email_mod
    from app.modules.alerts import raise_alert

    monkeypatch.setattr(config.settings, "alert_email_to", None)
    monkeypatch.setattr(email_mod.smtplib, "SMTP", lambda *a, **k: 1 / 0)

    raise_alert("test_kind", "hello")  # must not attempt SMTP

    assert _FakeSMTP.instances == []


def test_raise_alert_emails_when_configured(monkeypatch):
    from app import config
    from app.integrations import email as email_mod
    from app.modules.alerts import raise_alert

    monkeypatch.setattr(config.settings, "alert_email_to", "ops@example.com")
    monkeypatch.setattr(config.settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(email_mod.smtplib, "SMTP", _FakeSMTP)

    raise_alert("zero_reach", "blog has zero reach", product_id=1)

    sent = _FakeSMTP.instances[0].sent
    assert len(sent) == 1
    assert sent[0]["To"] == "ops@example.com"
    assert "zero_reach" in sent[0]["Subject"]
    assert "product_id=1" in sent[0].get_content()


def test_alert_email_redacts_registered_secrets(monkeypatch):
    """Alert emails bypass the log-record redactor — the boundary redact() must scrub instead.
    Regression: OAuth-refresh alert context carries raw provider error strings (publish.py's
    _fence_channel passes error=...) which can embed token material."""
    from app import config
    from app.integrations import email as email_mod
    from app.modules.alerts import raise_alert
    from app.secrets.vault import register_secret

    monkeypatch.setattr(config.settings, "alert_email_to", "ops@example.com")
    monkeypatch.setattr(config.settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(email_mod.smtplib, "SMTP", _FakeSMTP)

    register_secret("tok-supersecret-123")
    raise_alert("oauth_refresh_failed", "refresh blew up", error="401: tok-supersecret-123")

    sent = _FakeSMTP.instances[0].sent
    assert len(sent) == 1
    body = sent[0].get_content()
    assert "tok-supersecret-123" not in body
    assert "401" in body  # the useful part of the error survives


def test_run_heartbeat_sends_digest_email_when_configured(engine, monkeypatch):
    from app import config
    from app.integrations import email as email_mod

    monkeypatch.setattr(config.settings, "alert_email_to", "ops@example.com")
    monkeypatch.setattr(config.settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(email_mod.smtplib, "SMTP", _FakeSMTP)

    p = _make_product(engine)
    ch = _make_channel(engine, p.id, ChannelType.BLOG)
    _add_item(
        engine,
        p.id,
        ch.id,
        status=ContentItemStatus.PUBLISHED,
        published_at=NOW - timedelta(hours=1),
    )
    _add_impressions(engine, p.id, ch.id, 2, NOW - timedelta(hours=1))

    with Session(engine) as s:
        run_heartbeat(s, NOW)

    digests = [m for inst in _FakeSMTP.instances for m in inst.sent if "heartbeat" in m["Subject"]]
    assert len(digests) == 1
    body = digests[0].get_content()
    assert "published=1" in body
    assert "reach=2" in body
    assert "No alerts." in body


# --- API ---------------------------------------------------------------------


def test_heartbeat_api_missing_product_404(client):
    assert client.get("/api/private/metrics/999/heartbeat").status_code == 404


def test_heartbeat_api_returns_recent_digests(engine, client):
    p = _make_product(engine)
    ch = _make_channel(engine, p.id, ChannelType.BLOG)
    _add_item(
        engine,
        p.id,
        ch.id,
        status=ContentItemStatus.PUBLISHED,
        published_at=NOW - timedelta(hours=1),
    )
    _add_impressions(engine, p.id, ch.id, 2, NOW - timedelta(hours=1))

    with Session(engine) as s:
        run_heartbeat(s, NOW)

    resp = client.get(f"/api/private/metrics/{p.id}/heartbeat")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["digests"]) == 1
    digest = body["digests"][0]
    assert digest["channels"][0]["channel_type"] == "blog"
    assert digest["channels"][0]["published"] == 1
    assert digest["channels"][0]["reach"] == 2
    assert digest["alerts"] == []
    assert digest["window_end"]


def test_heartbeat_api_empty_product_returns_no_digests(engine, client):
    p = _make_product(engine)
    resp = client.get(f"/api/private/metrics/{p.id}/heartbeat")
    assert resp.status_code == 200
    assert resp.json() == {"digests": []}
