"""S2.5: attribution chain — the webhook join that closes UTM → lead → Stripe → paid metric.

The full chain end-to-end: a UTM visit + a lead carry a `first_touch_token`; a Stripe
`checkout.session.completed` webhook arrives with that token as `client_reference_id`; the webhook
joins it back to the lead and writes `metric_event(stage=paid)` attributed to the product. The
webhook body is signed exactly as Stripe signs it (stdlib HMAC), so no `stripe` SDK / mocking lib.
"""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app import config, workspace
from app.api.public import ratelimit
from app.db import get_session
from app.main import create_app
from app.models import MonetizationModel, Product
from app.models.metric_event import MetricEvent, MetricStage

SECRET = "whsec_testsecret"


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
    monkeypatch.setattr(config.settings, "stripe_webhook_secret", SecretStr(SECRET))
    ratelimit.reset()

    def _session_override():
        with Session(engine) as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _session_override
    yield app, engine


def _make_product(engine) -> int:
    with Session(engine) as s:
        product = Product(
            name="Auto Author",
            slug="auto-author",
            monetization_model=MonetizationModel.CC_SUB,
            marketing_domain="https://autoauthor.app",
        )
        s.add(product)
        s.commit()
        return product.id


def _sign(payload: bytes, *, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.{payload.decode()}".encode()
    sig = hmac.new(SECRET.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _completed_event(
    *, token: str | None, product_id: int | None, session_id="cs_test_1", amount_total=2900
) -> bytes:
    metadata: dict = {}
    if token is not None:
        metadata["first_touch_token"] = token
    if product_id is not None:
        metadata["product_id"] = str(product_id)
    return json.dumps(
        {
            "id": "evt_1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": session_id,
                    "object": "checkout.session",
                    "client_reference_id": token,
                    "amount_total": amount_total,
                    "metadata": metadata,
                }
            },
        }
    ).encode()


def _post_webhook(client: TestClient, payload: bytes, **sign_kwargs):
    return client.post(
        "/api/stripe/webhook",
        content=payload,
        headers={"Stripe-Signature": _sign(payload, **sign_kwargs)},
    )


def _metrics(engine) -> list[MetricEvent]:
    with Session(engine) as s:
        return list(s.exec(select(MetricEvent)))


def test_full_chain_visit_lead_paid_metric(ctx):
    """UTM visit → lead → simulated paid subscription → attributed paid metric_event."""
    app, engine = ctx
    product_id = _make_product(engine)
    token = "tok-attrib-1"

    with TestClient(app) as client:
        # 1. UTM visit sets the first-touch token.
        assert (
            client.post(
                "/api/funnel/auto-author/visit",
                json={"first_touch_token": token, "utm_source": "reddit", "utm_campaign": "launch"},
            ).status_code
            == 201
        )
        # 2. Lead capture persists the token onto the lead row.
        assert (
            client.post(
                "/api/funnel/auto-author/lead",
                json={"email": "buyer@example.com", "first_touch_token": token},
            ).status_code
            == 201
        )
        # 3. Stripe fires checkout.session.completed carrying the token as client_reference_id.
        resp = _post_webhook(client, _completed_event(token=token, product_id=product_id))

    assert resp.status_code == 200
    rows = _metrics(engine)
    assert len(rows) == 1
    metric = rows[0]
    assert metric.stage == MetricStage.PAID
    assert metric.product_id == product_id  # joined via token → lead → product
    assert metric.value == 2900
    assert metric.source == "stripe:cs_test_1"


def test_attribution_falls_back_to_metadata_product(ctx):
    """No matching lead (e.g. direct/expired cookie) → attribute via checkout metadata."""
    app, engine = ctx
    product_id = _make_product(engine)

    with TestClient(app) as client:
        resp = _post_webhook(client, _completed_event(token="no-such-lead", product_id=product_id))

    assert resp.status_code == 200
    rows = _metrics(engine)
    assert len(rows) == 1
    assert rows[0].product_id == product_id
    assert rows[0].stage == MetricStage.PAID


def test_redelivered_event_is_idempotent(ctx):
    """Stripe redelivers events; the same checkout session must not double-count revenue."""
    app, engine = ctx
    product_id = _make_product(engine)
    payload = _completed_event(token="t", product_id=product_id)

    with TestClient(app) as client:
        assert _post_webhook(client, payload).status_code == 200
        assert _post_webhook(client, payload).status_code == 200

    assert len(_metrics(engine)) == 1


def test_unattributable_session_records_nothing(ctx):
    """No lead and no metadata product_id → ack Stripe (200) but write no metric."""
    app, engine = ctx
    _make_product(engine)

    with TestClient(app) as client:
        resp = _post_webhook(client, _completed_event(token=None, product_id=None))

    assert resp.status_code == 200
    assert _metrics(engine) == []


def test_non_paid_event_ignored(ctx):
    """A non-checkout.session.completed event is acknowledged but records no paid metric."""
    app, engine = ctx
    _make_product(engine)
    payload = json.dumps({"id": "evt_2", "type": "payment_intent.created", "data": {}}).encode()

    with TestClient(app) as client:
        resp = _post_webhook(client, payload)

    assert resp.status_code == 200
    assert _metrics(engine) == []


def test_source_unique_constraint_enforced(ctx):
    """The DB-level backstop: two paid metrics for the same Stripe session can't both persist."""
    from sqlalchemy.exc import IntegrityError

    _, engine = ctx
    with Session(engine) as s:
        s.add(MetricEvent(product_id=1, stage=MetricStage.PAID, value=2900, source="stripe:cs_dup"))
        s.commit()
        s.add(MetricEvent(product_id=1, stage=MetricStage.PAID, value=2900, source="stripe:cs_dup"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_invalid_signature_records_nothing(ctx):
    """Signature still gates the handler — a forged body must not write a metric."""
    app, engine = ctx
    product_id = _make_product(engine)
    payload = _completed_event(token="t", product_id=product_id)

    with TestClient(app) as client:
        resp = client.post(
            "/api/stripe/webhook",
            content=payload,
            headers={"Stripe-Signature": "t=1,v1=forged"},
        )

    assert resp.status_code == 400
    assert _metrics(engine) == []
