"""S2.3: Stripe product+price setup → persists product.stripe_price_id.

Deterministic unit tests drive the worker wiring, persistence, idempotency, and the cc_sub-only /
price-required constraints with no network (the Stripe call is injected). The integration test
makes a real Stripe test-mode call and is skipped unless SME_STRIPE_API_KEY is set.
"""

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app import worker
from app.config import settings
from app.models import JobStatus, LifecycleState, MonetizationModel, Product
from app.modules.setup import stripe_setup as stripe_mod
from app.worker import enqueue, run_due_jobs


@pytest.fixture
def session(tmp_path):
    db = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _make_priced_product(
    session,
    *,
    model=MonetizationModel.CC_SUB,
    price=2900,
    interval="month",
    state=LifecycleState.SETUP_READY,
):
    product = Product(
        name="Auto Author",
        slug="auto-author",
        monetization_model=model,
        price_amount_cents=price,
        price_interval=interval,
        lifecycle_state=state,
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


# ---- persistence ---------------------------------------------------------------------------


def test_setup_persists_stripe_price_id(session):
    product = _make_priced_product(session)
    job = enqueue(session, "stripe_setup", product_id=product.id)
    captured = {}

    def _create(name, amount, interval):
        captured["args"] = (name, amount, interval)
        return "price_test_123"

    cost = stripe_mod.setup_stripe(job, session, create=_create)

    assert cost == 0  # no token spend
    session.commit()  # the worker commits after the handler; mimic that before asserting
    session.refresh(product)
    assert product.stripe_price_id == "price_test_123"
    assert captured["args"] == ("Auto Author", 2900, "month")


def test_setup_is_idempotent(session):
    product = _make_priced_product(session)
    product.stripe_price_id = "price_existing"
    session.add(product)
    session.commit()
    job = enqueue(session, "stripe_setup", product_id=product.id)

    def _boom(*a):  # must not be reached — already configured
        raise AssertionError("create called for an already-configured product")

    stripe_mod.setup_stripe(job, session, create=_boom)
    session.refresh(product)
    assert product.stripe_price_id == "price_existing"  # unchanged


def test_non_cc_sub_raises(session):
    product = _make_priced_product(session, model=MonetizationModel.TRIAL)
    job = enqueue(session, "stripe_setup", product_id=product.id)

    def _boom(*a):
        raise AssertionError("create called for a non-cc_sub product")

    with pytest.raises(RuntimeError, match="cc_sub"):
        stripe_mod.setup_stripe(job, session, create=_boom)


def test_unsupported_interval_raises(session):
    # price_interval is a free-string column; a manual edit to e.g. "weekly" must fail before any
    # Stripe call (else a Product is created then Price creation rejects it).
    product = _make_priced_product(session, interval="weekly")
    job = enqueue(session, "stripe_setup", product_id=product.id)

    def _boom(*a):
        raise AssertionError("create called with an unsupported interval")

    with pytest.raises(RuntimeError, match="interval"):
        stripe_mod.setup_stripe(job, session, create=_boom)


def test_missing_price_raises(session):
    product = _make_priced_product(session, price=None, interval=None)
    job = enqueue(session, "stripe_setup", product_id=product.id)

    def _boom(*a):
        raise AssertionError("create called without a price")

    with pytest.raises(RuntimeError, match="no price"):
        stripe_mod.setup_stripe(job, session, create=_boom)


# ---- worker path ---------------------------------------------------------------------------


def test_worker_runs_handler(session, monkeypatch):
    product = _make_priced_product(session)
    monkeypatch.setattr(stripe_mod, "_CREATE", lambda n, a, i: "price_from_worker")
    job = enqueue(session, "stripe_setup", product_id=product.id)

    assert "stripe_setup" in worker._HANDLERS  # registered at import
    run_due_jobs(session)

    session.refresh(job)
    session.refresh(product)
    assert job.status == JobStatus.DONE
    assert product.stripe_price_id == "price_from_worker"


# ---- private trigger route -----------------------------------------------------------------


def _client_for(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_route_enqueues_stripe_setup(session):
    product = _make_priced_product(session)
    with _client_for(session) as client:
        resp = client.post(f"/api/private/setup/{product.id}/stripe")
    assert resp.status_code == 202
    assert resp.json()["status"] == JobStatus.QUEUED


def test_route_404_for_missing_product(session):
    with _client_for(session) as client:
        resp = client.post("/api/private/setup/999/stripe")
    assert resp.status_code == 404


def test_route_409_when_not_setup_ready(session):
    # Don't create paid Stripe resources before the strategy is approved (default state is DRAFT).
    product = _make_priced_product(session, state=LifecycleState.DRAFT)
    with _client_for(session) as client:
        resp = client.post(f"/api/private/setup/{product.id}/stripe")
    assert resp.status_code == 409


def test_pricing_edit_clears_stripe_price_id(session):
    # A price change after setup must invalidate the old Stripe Price so Checkout can't charge it.
    product = _make_priced_product(session)
    product.stripe_price_id = "price_old"
    session.add(product)
    session.commit()
    with _client_for(session) as client:
        resp = client.patch(
            f"/api/private/products/{product.id}", json={"price_amount_cents": 4900}
        )
    assert resp.status_code == 200
    assert resp.json()["stripe_price_id"] is None
    assert resp.json()["price_amount_cents"] == 4900


def test_noop_resubmit_keeps_stripe_price_id(session):
    # Dashboards often POST the whole product; resubmitting the same price keeps checkout working.
    product = _make_priced_product(session)
    product.stripe_price_id = "price_keep"
    session.add(product)
    session.commit()
    with _client_for(session) as client:
        resp = client.patch(
            f"/api/private/products/{product.id}",
            json={"price_amount_cents": 2900, "price_interval": "month", "description": "edited"},
        )
    assert resp.status_code == 200
    assert resp.json()["stripe_price_id"] == "price_keep"


def test_switching_off_cc_sub_clears_stripe_price_id(session):
    # Checkout must not bill a non-cc_sub product; clear its Stripe price on the model change.
    product = _make_priced_product(session)
    product.stripe_price_id = "price_old"
    session.add(product)
    session.commit()
    with _client_for(session) as client:
        resp = client.patch(
            f"/api/private/products/{product.id}", json={"monetization_model": "trial"}
        )
    assert resp.status_code == 200
    assert resp.json()["stripe_price_id"] is None


def test_route_400_when_no_price(session):
    product = _make_priced_product(session, price=None, interval=None)
    with _client_for(session) as client:
        resp = client.post(f"/api/private/setup/{product.id}/stripe")
    assert resp.status_code == 400


def test_route_400_when_not_cc_sub(session):
    product = _make_priced_product(session, model=MonetizationModel.FREEMIUM)
    with _client_for(session) as client:
        resp = client.post(f"/api/private/setup/{product.id}/stripe")
    assert resp.status_code == 400


def test_route_400_when_interval_unsupported(session):
    product = _make_priced_product(session, interval="weekly")
    with _client_for(session) as client:
        resp = client.post(f"/api/private/setup/{product.id}/stripe")
    assert resp.status_code == 400


# ---- real-API integration (key-gated) ------------------------------------------------------


@pytest.mark.skipif(
    settings.stripe_api_key is None,
    reason="requires SME_STRIPE_API_KEY (real Stripe test-mode call); set it in backend/.env",
)
def test_integration_real_stripe_setup(session):
    product = _make_priced_product(session)
    job = enqueue(session, "stripe_setup", product_id=product.id)

    stripe_mod.setup_stripe(job, session, create=stripe_mod._real_create)

    session.commit()
    session.refresh(product)
    assert product.stripe_price_id and product.stripe_price_id.startswith("price_")
