"""Metrics API (private dashboard, story S6.1 — attributed funnel + revenue).

Surfaces the per-product funnel rollup (stage totals + per-channel/content-item attribution rows)
so the operator can see impressions → visits → signups → paid → revenue, joinable back to the
channel/content item that drove each conversion. Per-product only; portfolio roll-up is deferred
until there's more than one product (TECH_SPEC §14).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.db import get_session
from app.models import Product
from app.modules.metrics.funnel import funnel_rollup

router = APIRouter(prefix="/metrics", tags=["metrics"])

SessionDep = Annotated[Session, Depends(get_session)]


def _require_product(session: Session, product_id: int) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    return product


@router.get("/{product_id}/funnel")
def get_funnel(product_id: int, session: SessionDep) -> dict:
    product = _require_product(session, product_id)
    return funnel_rollup(session, product)
