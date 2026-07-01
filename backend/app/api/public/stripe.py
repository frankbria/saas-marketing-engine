"""Public Stripe webhook: receive + verify (S2.2), then join to attribution (S2.5).

Verifies Stripe's signature with stdlib HMAC-SHA256 (the documented `t=…,v1=…`
scheme) plus a timestamp tolerance to blunt replay. No `stripe` SDK dependency — the
signature-verified body is parsed as plain JSON. On `checkout.session.completed`, the
`client_reference_id` (the funnel's first-touch token) is joined back to the lead row →
`metric_event(stage=paid)`, closing the attribution chain (TECH_SPEC §6.6).
"""

import hashlib
import hmac
import json
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import settings
from app.db import get_session
from app.models.funnel_event import FunnelEvent, FunnelEventType
from app.models.metric_event import MetricEvent, MetricStage
from app.modules.metrics.utm import resolve_attribution

router = APIRouter(prefix="/stripe", tags=["stripe"])

SessionDep = Annotated[Session, Depends(get_session)]

# Stripe's default replay window. Reject signatures whose timestamp is further off than this.
_TOLERANCE_SECONDS = 300


def _parse_signature_header(header: str) -> tuple[int | None, list[str]]:
    """Pull the timestamp `t` and all `v1` signatures out of a Stripe-Signature header."""
    timestamp: int | None = None
    v1: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                return None, []
        elif key == "v1":
            v1.append(value)
    return timestamp, v1


def verify_signature(payload: bytes, header: str | None, secret: str, *, now: int) -> bool:
    """True iff `header` carries a fresh, valid Stripe signature for `payload`."""
    if not header:
        return False
    timestamp, signatures = _parse_signature_header(header)
    if timestamp is None or not signatures:
        return False
    if abs(now - timestamp) > _TOLERANCE_SECONDS:
        return False
    signed = f"{timestamp}.{payload.decode('utf-8', 'replace')}".encode()
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in signatures)


def _attribute_paid_metric(event: dict, session: Session) -> None:
    """Join a `checkout.session.completed` event to its lead → write `metric_event(stage=paid)`.

    Attribution is `client_reference_id` (first-touch token) → lead `FunnelEvent` → `product_id`,
    falling back to the checkout's `metadata.product_id` when the cookie/lead is missing. When the
    lead resolved, its UTM fields resolve `channel_id`/`content_item_id` too (S6.1,
    `resolve_attribution` — the same join the funnel rollup uses), so a paid row is joinable back to
    the exact channel/content item that drove it wherever the lead carried that data. An
    unattributable session is acknowledged but records nothing — never fail the webhook back to
    Stripe. Idempotent on the checkout session id (Stripe redelivers events).
    """
    obj = event.get("data", {}).get("object", {})
    token = obj.get("client_reference_id")
    metadata = obj.get("metadata") or {}
    session_id = obj.get("id")
    source = f"stripe:{session_id}" if session_id else "stripe"

    # Idempotency fast-path: a redelivered session must not double-count revenue. The unique
    # constraint on `source` is the race-proof backstop (handled at the insert below).
    if session.exec(select(MetricEvent).where(MetricEvent.source == source)).first():
        return

    product_id: int | None = None
    lead: FunnelEvent | None = None
    if token:
        lead = session.exec(
            select(FunnelEvent).where(
                FunnelEvent.first_touch_token == token,
                FunnelEvent.event_type == FunnelEventType.LEAD,
            )
        ).first()
        if lead is not None:
            product_id = lead.product_id
    if product_id is None:
        try:
            product_id = int(metadata.get("product_id"))
        except (TypeError, ValueError):
            product_id = None
    if product_id is None:
        return  # unattributable — no lead and no metadata product_id to fall back to

    channel_id: int | None = None
    content_item_id: int | None = None
    if lead is not None:
        channel_id, content_item_id = resolve_attribution(
            session, product_id, lead.utm_source, lead.utm_content
        )

    session.add(
        MetricEvent(
            product_id=product_id,
            channel_id=channel_id,
            content_item_id=content_item_id,
            stage=MetricStage.PAID,
            value=int(obj.get("amount_total") or 0),
            source=source,
        )
    )
    try:
        session.commit()
    except IntegrityError:
        # A concurrent redelivery won the race and already recorded this session — that's the
        # idempotent outcome we want, not an error.
        session.rollback()


@router.post("/webhook")
async def stripe_webhook(request: Request, session: SessionDep) -> dict[str, bool]:
    if settings.stripe_webhook_secret is None:
        # Fail loudly rather than silently accept an unauthenticated webhook.
        raise HTTPException(status_code=503, detail="stripe webhook not configured")

    payload = await request.body()
    header = request.headers.get("stripe-signature")
    secret = settings.stripe_webhook_secret.get_secret_value()
    if not verify_signature(payload, header, secret, now=int(time.time())):
        raise HTTPException(status_code=400, detail="invalid signature")

    event = json.loads(payload)  # body is signature-verified above
    if event.get("type") == "checkout.session.completed":
        _attribute_paid_metric(event, session)
    return {"received": True}
