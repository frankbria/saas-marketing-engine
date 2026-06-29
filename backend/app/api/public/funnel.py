"""Public funnel-ingest endpoints (S2.2): visit + lead.

Internet-facing, rate-limited (dependency), strictly validated (pydantic), and
CORS-scoped to the product origin (middleware in cors.py). Records raw FunnelEvent
rows; the welcome email (S2.4) and attribution join (S2.5) build on them.
"""

import re
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlmodel import Session, select

from app.api.public.ratelimit import enforce_rate_limit
from app.config import settings
from app.db import get_session
from app.integrations import stripe_api
from app.models.funnel_event import FunnelEvent, FunnelEventType
from app.models.product import Product

router = APIRouter(prefix="/funnel", tags=["funnel"])

SessionDep = Annotated[Session, Depends(get_session)]
RateLimited = Depends(enforce_rate_limit)

# Checkout-session creator seam. The real impl calls Stripe; tests override it via
# `app.dependency_overrides[get_checkout_creator]` so no network/mocking-library is needed.
CheckoutCreator = Callable[..., str]


def get_checkout_creator() -> CheckoutCreator:
    return stripe_api.create_checkout_session


# Deliberately permissive — reject the obviously-broken, not gatekeep deliverability.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class VisitCreate(BaseModel):
    model_config = {"extra": "forbid"}  # unknown fields → 422 (strict validation, AC)

    first_touch_token: str | None = Field(default=None, max_length=128)
    utm_source: str | None = Field(default=None, max_length=256)
    utm_medium: str | None = Field(default=None, max_length=256)
    utm_campaign: str | None = Field(default=None, max_length=256)
    utm_content: str | None = Field(default=None, max_length=256)
    utm_term: str | None = Field(default=None, max_length=256)


class LeadCreate(VisitCreate):
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        v = v.strip()
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v.lower()


class CheckoutCreate(VisitCreate):
    # The site sends client_reference_id explicitly; fall back to first_touch_token if absent.
    client_reference_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _require_attribution(self) -> "CheckoutCreate":
        # The funnel's point is attribution: a checkout with no token can't be joined to a lead at
        # the paid webhook (S2.5). The site always sends one; reject malformed/direct requests.
        if not (self.first_touch_token or self.client_reference_id):
            raise ValueError("first_touch_token or client_reference_id is required")
        return self


def _site_base_url(product: Product) -> str:
    """Absolute base URL for Checkout success/cancel redirects (product site, else the API host)."""
    domain = (product.marketing_domain or "").strip().rstrip("/")
    if not domain:
        return settings.public_api_base_url.rstrip("/")
    return domain if "://" in domain else f"https://{domain}"


def _product_or_404(session: Session, slug: str) -> Product:
    product = session.exec(select(Product).where(Product.slug == slug)).first()
    if product is None:
        raise HTTPException(status_code=404, detail="unknown product")
    return product


def _record(
    slug: str, event_type: FunnelEventType, payload: VisitCreate, session: Session
) -> dict[str, str]:
    product = _product_or_404(session, slug)
    event = FunnelEvent(product_id=product.id, event_type=event_type, **payload.model_dump())
    session.add(event)
    session.commit()
    return {"status": "recorded"}


@router.post("/{slug}/visit", status_code=201, dependencies=[RateLimited])
def record_visit(slug: str, payload: VisitCreate, session: SessionDep) -> dict[str, str]:
    return _record(slug, FunnelEventType.VISIT, payload, session)


@router.post("/{slug}/lead", status_code=201, dependencies=[RateLimited])
def record_lead(slug: str, payload: LeadCreate, session: SessionDep) -> dict[str, str]:
    return _record(slug, FunnelEventType.LEAD, payload, session)


@router.post("/{slug}/checkout", dependencies=[RateLimited])
def start_checkout(
    slug: str,
    payload: CheckoutCreate,
    session: SessionDep,
    create: Annotated[CheckoutCreator, Depends(get_checkout_creator)],
) -> dict[str, str]:
    """Start a Stripe Checkout subscription, carrying the funnel's attribution token."""
    product = _product_or_404(session, slug)
    if not product.stripe_price_id:
        raise HTTPException(status_code=409, detail="stripe not configured for this product")
    base = _site_base_url(product)
    url = create(
        price_id=product.stripe_price_id,
        client_reference_id=payload.client_reference_id or payload.first_touch_token,
        success_url=f"{base}/?checkout=success",
        cancel_url=f"{base}/?checkout=cancel",
        metadata={"first_touch_token": payload.first_touch_token, "product_id": product.id},
    )
    return {"url": url}
