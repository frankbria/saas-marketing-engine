"""Metrics API (private dashboard, story S6.1 — attributed funnel + revenue).

Surfaces the per-product funnel rollup (stage totals + per-channel/content-item attribution rows)
so the operator can see impressions → visits → signups → paid → revenue, joinable back to the
channel/content item that drove each conversion. Per-product only; portfolio roll-up is deferred
until there's more than one product (TECH_SPEC §14).
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import HeartbeatDigest, Product
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


@router.get("/{product_id}/heartbeat")
def get_heartbeat(product_id: int, session: SessionDep, limit: int = 14) -> dict:
    """Recent heartbeat digests + alerts (S6.2) — the operator's Flower replacement.

    Newest first; `limit` defaults to two weeks of daily digests (the PRD's "unattended ≥2
    weeks" horizon).
    """
    _require_product(session, product_id)
    rows = session.exec(
        select(HeartbeatDigest)
        .where(HeartbeatDigest.product_id == product_id)
        .order_by(HeartbeatDigest.window_end.desc())  # type: ignore[attr-defined]
        .limit(limit)
    ).all()
    return {
        "digests": [
            {
                "id": row.id,
                "window_start": row.window_start.isoformat(),
                "window_end": row.window_end.isoformat(),
                "channels": json.loads(row.digest_json)["channels"],
                "alerts": json.loads(row.alerts_json),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }
