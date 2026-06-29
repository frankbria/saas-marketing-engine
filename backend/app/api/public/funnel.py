"""Public funnel-ingest endpoints (S2.2): visit + lead.

Internet-facing, rate-limited (dependency), strictly validated (pydantic), and
CORS-scoped to the product origin (middleware in cors.py). Records raw FunnelEvent
rows; the welcome email (S2.4) and attribution join (S2.5) build on them.
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, select

from app.api.public.ratelimit import enforce_rate_limit
from app.db import get_session
from app.models.funnel_event import FunnelEvent, FunnelEventType
from app.models.product import Product

router = APIRouter(prefix="/funnel", tags=["funnel"])

SessionDep = Annotated[Session, Depends(get_session)]
RateLimited = Depends(enforce_rate_limit)

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
