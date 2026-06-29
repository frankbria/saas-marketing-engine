"""Stripe REST calls (S2.3) — stdlib only, mirroring the webhook's no-SDK choice.

The few resources S2.3 needs (a Product, a recurring Price, a Checkout Session) are plain
form-encoded POSTs to api.stripe.com authenticated with a bearer secret key. No `stripe` SDK and no
new runtime dependency — `urllib` does it, exactly as `app/api/public/stripe.py` verifies webhook
signatures with stdlib HMAC. Non-2xx responses raise loudly with the Stripe error body so a
misconfigured key or a rejected request never passes silently.

ponytail: no Idempotency-Key / retry / pagination — single test-mode calls in v1. Add an
Idempotency-Key header (and retry) when this drives live-mode money.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from app.config import settings

_API_BASE = "https://api.stripe.com/v1"
_TIMEOUT_SECONDS = 30


def _api_key() -> str:
    key = settings.stripe_api_key
    if key is None:
        raise RuntimeError("SME_STRIPE_API_KEY is not set; Stripe is not configured")
    return key.get_secret_value()


def _post(path: str, fields: list[tuple[str, str]]) -> dict:
    """POST form-encoded `fields` to the Stripe API; return the parsed JSON body.

    `fields` is a list of (key, value) pairs rather than a dict so Stripe's nested bracket keys
    (`recurring[interval]`, `line_items[0][price]`) round-trip exactly and can repeat.
    """
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        f"{_API_BASE}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            req, timeout=_TIMEOUT_SECONDS
        ) as resp:  # noqa: S310 — fixed https host
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Stripe API {path} failed ({exc.code}): {body}") from exc


def create_product(name: str) -> str:
    """Create a Stripe Product; return its id."""
    return _post("/products", [("name", name)])["id"]


def create_price(
    stripe_product_id: str, amount_cents: int, interval: str, *, currency: str = "usd"
) -> str:
    """Create a recurring Price under `stripe_product_id`; return its id."""
    return _post(
        "/prices",
        [
            ("product", stripe_product_id),
            ("unit_amount", str(amount_cents)),
            ("currency", currency),
            ("recurring[interval]", interval),
        ],
    )["id"]


def create_checkout_session(
    *,
    price_id: str,
    client_reference_id: str | None,
    success_url: str,
    cancel_url: str,
    metadata: dict[str, object] | None = None,
) -> str:
    """Create a subscription-mode Checkout Session; return its hosted `url`.

    `client_reference_id` and `metadata` carry the funnel's first-touch token so S2.5 can join the
    resulting `checkout.session.completed` webhook back to the lead.
    """
    fields: list[tuple[str, str]] = [
        ("mode", "subscription"),
        ("line_items[0][price]", price_id),
        ("line_items[0][quantity]", "1"),
        ("success_url", success_url),
        ("cancel_url", cancel_url),
    ]
    if client_reference_id:
        fields.append(("client_reference_id", client_reference_id))
    for key, value in (metadata or {}).items():
        if value is not None:
            fields.append((f"metadata[{key}]", str(value)))
    return _post("/checkout/sessions", fields)["url"]
