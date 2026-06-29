"""Public Stripe webhook (S2.2): receive + verify only.

Verifies Stripe's signature with stdlib HMAC-SHA256 (the documented `t=…,v1=…`
scheme) plus a timestamp tolerance to blunt replay. No `stripe` SDK dependency — S2.2
only needs to accept and authenticate the call. Event handling (checkout.session
.completed → attribution/metric_event) lands in S2.5.
"""

import hashlib
import hmac
import time

from fastapi import APIRouter, HTTPException, Request

from app.config import settings

router = APIRouter(prefix="/stripe", tags=["stripe"])

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


@router.post("/webhook")
async def stripe_webhook(request: Request) -> dict[str, bool]:
    if settings.stripe_webhook_secret is None:
        # Fail loudly rather than silently accept an unauthenticated webhook.
        raise HTTPException(status_code=503, detail="stripe webhook not configured")

    payload = await request.body()
    header = request.headers.get("stripe-signature")
    secret = settings.stripe_webhook_secret.get_secret_value()
    if not verify_signature(payload, header, secret, now=int(time.time())):
        raise HTTPException(status_code=400, detail="invalid signature")

    # ponytail: receipt only in S2.2. S2.5 parses the event and joins it to a lead.
    return {"received": True}
