"""S2.3: public checkout endpoint — starts a Stripe Checkout session carrying attribution.

The Stripe call is injected via the FastAPI dependency `get_checkout_creator` (overridden per test),
so the offline tests assert the request the engine *would* send Stripe — price, redirect URLs, and
the client_reference_id/metadata that S2.5 joins on — with no network. The integration test hits
Stripe test mode and is skipped unless SME_STRIPE_API_KEY is set.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app import workspace
from app.api.public import ratelimit
from app.api.public.funnel import get_checkout_creator
from app.config import settings
from app.db import get_session
from app.main import create_app
from app.models import MonetizationModel, Product


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
    yield app, engine


def _make_product(engine, *, price_id="price_123", domain="https://autoauthor.app"):
    with Session(engine) as s:
        product = Product(
            name="Auto Author",
            slug="auto-author",
            monetization_model=MonetizationModel.CC_SUB,
            price_amount_cents=2900,
            price_interval="month",
            stripe_price_id=price_id,
            marketing_domain=domain,
        )
        s.add(product)
        s.commit()


def test_checkout_returns_url_and_passes_attribution(ctx):
    app, engine = ctx
    _make_product(engine)
    captured = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return "https://checkout.stripe.com/c/pay/cs_test_abc"

    app.dependency_overrides[get_checkout_creator] = lambda: _fake_create
    with TestClient(app) as client:
        resp = client.post(
            "/api/funnel/auto-author/checkout",
            json={"first_touch_token": "tok-xyz", "client_reference_id": "tok-xyz"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"url": "https://checkout.stripe.com/c/pay/cs_test_abc"}
    assert captured["price_id"] == "price_123"
    assert captured["client_reference_id"] == "tok-xyz"  # the attribution key S2.5 joins on
    assert captured["metadata"]["first_touch_token"] == "tok-xyz"
    assert captured["success_url"] == "https://autoauthor.app/?checkout=success"
    assert captured["cancel_url"] == "https://autoauthor.app/?checkout=cancel"


def test_checkout_falls_back_to_first_touch_token(ctx):
    app, engine = ctx
    _make_product(engine)
    captured = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return "https://checkout.stripe.com/c/pay/cs_test_def"

    app.dependency_overrides[get_checkout_creator] = lambda: _fake_create
    with TestClient(app) as client:
        resp = client.post(
            "/api/funnel/auto-author/checkout", json={"first_touch_token": "only-token"}
        )

    assert resp.status_code == 200
    assert captured["client_reference_id"] == "only-token"


def test_checkout_409_when_stripe_not_configured(ctx):
    app, engine = ctx
    _make_product(engine, price_id=None)

    def _boom(**kwargs):
        raise AssertionError("Stripe must not be called without a price id")

    app.dependency_overrides[get_checkout_creator] = lambda: _boom
    with TestClient(app) as client:
        resp = client.post("/api/funnel/auto-author/checkout", json={"first_touch_token": "t"})
    assert resp.status_code == 409


def test_checkout_404_for_unknown_slug(ctx):
    app, _ = ctx
    with TestClient(app) as client:
        resp = client.post("/api/funnel/nope/checkout", json={"first_touch_token": "t"})
    assert resp.status_code == 404


def test_checkout_rejects_extra_fields(ctx):
    app, engine = ctx
    _make_product(engine)
    with TestClient(app) as client:
        resp = client.post("/api/funnel/auto-author/checkout", json={"surprise": "value"})
    assert resp.status_code == 422


def test_checkout_requires_attribution_token(ctx):
    # No first_touch_token / client_reference_id → the paid webhook couldn't join to a lead. Reject.
    app, engine = ctx
    _make_product(engine)

    def _boom(**kwargs):
        raise AssertionError("Stripe must not be called for an unattributable checkout")

    app.dependency_overrides[get_checkout_creator] = lambda: _boom
    with TestClient(app) as client:
        resp = client.post("/api/funnel/auto-author/checkout", json={})
    assert resp.status_code == 422


def test_checkout_base_url_falls_back_to_api_when_no_domain(ctx, monkeypatch):
    app, engine = ctx
    _make_product(engine, domain=None)
    monkeypatch.setattr(
        "app.api.public.funnel.settings.public_api_base_url", "http://localhost:8010"
    )
    captured = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return "https://checkout.stripe.com/c/pay/cs_test_ghi"

    app.dependency_overrides[get_checkout_creator] = lambda: _fake_create
    with TestClient(app) as client:
        resp = client.post("/api/funnel/auto-author/checkout", json={"first_touch_token": "t"})
    assert resp.status_code == 200
    assert captured["success_url"] == "http://localhost:8010/?checkout=success"


# ---- real-API integration (key-gated) ------------------------------------------------------


@pytest.mark.skipif(
    settings.stripe_api_key is None,
    reason="requires SME_STRIPE_API_KEY (real Stripe test-mode call); set it in backend/.env",
)
def test_integration_real_checkout(ctx):
    from app.integrations import stripe_api

    app, engine = ctx
    stripe_product_id = stripe_api.create_product("Auto Author (test)")
    price_id = stripe_api.create_price(stripe_product_id, 2900, "month")
    _make_product(engine, price_id=price_id)

    with TestClient(app) as client:
        resp = client.post(
            "/api/funnel/auto-author/checkout", json={"first_touch_token": "tok-real"}
        )
    assert resp.status_code == 200
    assert resp.json()["url"].startswith("https://")
