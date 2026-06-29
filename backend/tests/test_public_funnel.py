"""S2.2: public funnel-ingest endpoints — validation, persistence, per-product CORS, rate limit."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app import workspace
from app.api.public import ratelimit
from app.db import get_session
from app.main import create_app
from app.models.funnel_event import FunnelEvent, FunnelEventType


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(workspace.settings, "workspace_root", str(tmp_path / "ws"))
    ratelimit.reset()

    def _session_override():
        with Session(engine) as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _session_override
    with TestClient(app) as c:
        yield c, engine


def _make_product(
    client: TestClient, *, name="Auto Author", domain="https://autoauthor.app"
) -> str:
    resp = client.post(
        "/api/private/products",
        json={"name": name, "marketing_domain": domain},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["slug"]


def _events(engine) -> list[FunnelEvent]:
    with Session(engine) as s:
        return list(s.exec(select(FunnelEvent)))


def test_visit_records_event(ctx):
    client, engine = ctx
    slug = _make_product(client)
    resp = client.post(
        f"/api/funnel/{slug}/visit",
        json={"first_touch_token": "tok123", "utm_source": "reddit", "utm_campaign": "launch"},
    )
    assert resp.status_code == 201
    assert resp.json() == {"status": "recorded"}
    rows = _events(engine)
    assert len(rows) == 1
    assert rows[0].event_type == FunnelEventType.VISIT
    assert rows[0].first_touch_token == "tok123"
    assert rows[0].utm_source == "reddit"
    assert rows[0].email is None


def test_lead_records_event_with_email(ctx):
    client, engine = ctx
    slug = _make_product(client)
    resp = client.post(
        f"/api/funnel/{slug}/lead",
        json={"email": "User@Example.com", "first_touch_token": "tok9"},
    )
    assert resp.status_code == 201
    rows = _events(engine)
    assert len(rows) == 1
    assert rows[0].event_type == FunnelEventType.LEAD
    assert rows[0].email == "user@example.com"  # normalized lowercase


def test_lead_rejects_invalid_email(ctx):
    client, _ = ctx
    slug = _make_product(client)
    resp = client.post(f"/api/funnel/{slug}/lead", json={"email": "not-an-email"})
    assert resp.status_code == 422


def test_lead_requires_email(ctx):
    client, _ = ctx
    slug = _make_product(client)
    resp = client.post(f"/api/funnel/{slug}/lead", json={"first_touch_token": "x"})
    assert resp.status_code == 422


def test_unknown_slug_404(ctx):
    client, _ = ctx
    resp = client.post("/api/funnel/nope/visit", json={})
    assert resp.status_code == 404


def test_extra_fields_rejected(ctx):
    client, _ = ctx
    slug = _make_product(client)
    resp = client.post(f"/api/funnel/{slug}/visit", json={"surprise": "value"})
    assert resp.status_code == 422


def test_cors_echoes_matching_origin(ctx):
    client, _ = ctx
    slug = _make_product(client, domain="https://autoauthor.app")
    resp = client.post(
        f"/api/funnel/{slug}/visit",
        json={},
        headers={"Origin": "https://autoauthor.app"},
    )
    assert resp.status_code == 201
    assert resp.headers.get("access-control-allow-origin") == "https://autoauthor.app"
    assert resp.headers.get("vary") == "Origin"


def test_cors_denies_other_origin(ctx):
    client, _ = ctx
    slug = _make_product(client, domain="https://autoauthor.app")
    resp = client.post(
        f"/api/funnel/{slug}/visit",
        json={},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 201
    assert "access-control-allow-origin" not in resp.headers


def test_cors_preflight_options(ctx):
    client, _ = ctx
    slug = _make_product(client, domain="https://autoauthor.app")
    resp = client.options(
        f"/api/funnel/{slug}/lead",
        headers={
            "Origin": "https://autoauthor.app",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 204
    assert resp.headers.get("access-control-allow-origin") == "https://autoauthor.app"


def test_rate_limit_keys_are_pruned(monkeypatch):
    # Expired windows must not accumulate forever (codex P2): a rotating-slug client.
    ratelimit.reset()
    monkeypatch.setattr(ratelimit, "_MAX_KEYS", 3)
    now = 1000.0
    window = 60.0
    # 5 fully-expired entries (started > window ago) + we are over the cap of 3.
    for i in range(5):
        ratelimit._hits[f"old:{i}"] = (now - window - 1, 1)
    ratelimit._hits["fresh"] = (now, 1)
    ratelimit._prune_locked(now, window)
    assert "fresh" in ratelimit._hits
    assert not any(k.startswith("old:") for k in ratelimit._hits)
    ratelimit.reset()


def test_rate_limit_returns_429(ctx, monkeypatch):
    client, _ = ctx
    monkeypatch.setattr(ratelimit.settings, "rate_limit_requests", 3)
    monkeypatch.setattr(ratelimit.settings, "rate_limit_window_seconds", 60)
    slug = _make_product(client)
    codes = [client.post(f"/api/funnel/{slug}/visit", json={}).status_code for _ in range(5)]
    assert codes[:3] == [201, 201, 201]
    assert codes[3] == 429 and codes[4] == 429
