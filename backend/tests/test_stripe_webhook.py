"""S2.2: Stripe webhook — stdlib HMAC signature verification + timestamp tolerance."""

import hashlib
import hmac
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import config
from app.api.public import stripe as stripe_mod
from app.main import create_app

SECRET = "whsec_testsecret"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config.settings, "stripe_webhook_secret", SecretStr(SECRET))
    return TestClient(create_app())


def _sign(payload: bytes, secret: str = SECRET, *, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.{payload.decode()}".encode()
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_valid_signature_accepted(client):
    payload = b'{"id":"evt_1","type":"checkout.session.completed"}'
    resp = client.post(
        "/api/stripe/webhook",
        content=payload,
        headers={"Stripe-Signature": _sign(payload)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"received": True}


def test_invalid_signature_rejected(client):
    payload = b'{"id":"evt_1"}'
    resp = client.post(
        "/api/stripe/webhook",
        content=payload,
        headers={"Stripe-Signature": _sign(payload, secret="whsec_wrong")},
    )
    assert resp.status_code == 400


def test_missing_signature_rejected(client):
    resp = client.post("/api/stripe/webhook", content=b"{}")
    assert resp.status_code == 400


def test_stale_timestamp_rejected(client):
    payload = b'{"id":"evt_1"}'
    old = int(time.time()) - 10_000  # well outside the tolerance window
    resp = client.post(
        "/api/stripe/webhook",
        content=payload,
        headers={"Stripe-Signature": _sign(payload, timestamp=old)},
    )
    assert resp.status_code == 400


def test_unconfigured_secret_rejects_loudly(monkeypatch):
    monkeypatch.setattr(config.settings, "stripe_webhook_secret", None)
    client = TestClient(create_app())
    resp = client.post("/api/stripe/webhook", content=b"{}")
    assert resp.status_code == 503


def test_tampered_payload_rejected(client):
    payload = b'{"amount":100}'
    header = _sign(payload)
    tampered = b'{"amount":999999}'
    resp = client.post(
        "/api/stripe/webhook",
        content=tampered,
        headers={"Stripe-Signature": header},
    )
    assert resp.status_code == 400


def test_verify_signature_unit():
    payload = b"hello"
    assert stripe_mod.verify_signature(payload, _sign(payload), SECRET, now=int(time.time()))
    assert not stripe_mod.verify_signature(payload, "garbage", SECRET, now=int(time.time()))
    assert not stripe_mod.verify_signature(payload, None, SECRET, now=int(time.time()))
