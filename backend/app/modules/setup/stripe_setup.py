"""Stripe product+price setup handler (TECH_SPEC §6.3 / story S2.3).

Creates the Stripe Product + recurring Price for a cc_sub product and folds the resulting
`stripe_price_id` onto the product row (the one value Checkout needs). Mirrors pricing.py: the
external call is injected (`create`) so the worker wiring + persistence are testable without a
network call; the registered handler passes the real implementation. Idempotent — a product that
already has a `stripe_price_id` is left untouched so a re-run never creates duplicate Stripe
objects. cc_sub only; trial/freemium are enum values only in v1.

The handler returns 0 (Stripe setup spends no LLM tokens) and, like the other handlers, does not
commit — the worker commits the persisted price id + job status atomically.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlmodel import Session

from app.integrations import stripe_api
from app.models import MonetizationModel, Product
from app.worker import handler

# Stripe's recurring intervals that v1 emits. price_interval is a free-string column (owner-editable
# via PATCH), so guard it here before any Stripe call rather than orphaning a created Product when
# Price creation rejects an unsupported interval.
STRIPE_INTERVALS = frozenset({"month", "year"})

# create(name, amount_cents, interval) -> stripe_price_id
CreateFn = Callable[[str, int, str], str]


def _real_create(name: str, amount_cents: int, interval: str) -> str:
    stripe_product_id = stripe_api.create_product(name)
    return stripe_api.create_price(stripe_product_id, amount_cents, interval)


def setup_stripe(job, session: Session, *, create: CreateFn = _real_create) -> int:
    """Create the Stripe product+price for `job.product_id` and persist `stripe_price_id`."""
    if job.product_id is None:
        raise LookupError("stripe_setup job has no product_id")
    product = session.get(Product, job.product_id)
    if product is None:
        raise LookupError(f"product {job.product_id} not found")

    # Only cc_sub takes a Stripe subscription in v1 (matches the S1.3 pricing constraint). Refuse
    # rather than create a Stripe object nothing downstream would use.
    if product.monetization_model != MonetizationModel.CC_SUB:
        raise RuntimeError(
            f"stripe setup only supports cc_sub; product {product.id} is "
            f"{product.monetization_model} (trial/freemium unwired in v1)"
        )

    # Idempotent: never create a second Stripe product/price for one product. A pricing edit clears
    # stripe_price_id (products PATCH), so a genuine price change re-runs and recreates the Price.
    if product.stripe_price_id:
        return 0

    # The price is the Stripe Price's grounding (S2.3 depends on S1.3). Without it there's nothing
    # to bill — surface rather than creating a $0/unknown price.
    if product.price_amount_cents is None or product.price_interval is None:
        raise RuntimeError(
            f"product {product.id} has no price; run the pricing recommendation (S1.3) first"
        )
    if product.price_interval not in STRIPE_INTERVALS:
        raise RuntimeError(
            f"price_interval {product.price_interval!r} is not a Stripe recurring interval "
            f"(use one of {sorted(STRIPE_INTERVALS)})"
        )

    product.stripe_price_id = create(
        product.name, product.price_amount_cents, product.price_interval
    )
    product.updated_at = datetime.now(UTC)
    session.add(product)
    # No commit here: the worker commits the price id + job status atomically (matches pricing.py).
    return 0


# Indirection so tests can drive the full enqueue → run_due_jobs path with a stub creator
# (no network), while production uses the real Stripe implementation.
_CREATE: CreateFn = _real_create


@handler("stripe_setup")
def _stripe_setup_handler(job, session: Session) -> int:
    return setup_stripe(job, session, create=_CREATE)
