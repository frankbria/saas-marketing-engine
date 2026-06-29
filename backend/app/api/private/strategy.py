"""Strategy API (private dashboard, stories S1.1–S1.4).

Triggering the brief/brand/pricing enqueues a job_run; the S0.2 worker loop runs it
asynchronously (ingest + LLM can take a while). Returns 202 + the job id to poll.
S1.4 adds the synchronous review/edit + approve surface over the produced strategy.
"""

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlmodel import Session, select

from app.db import get_session
from app.models import LifecycleState, MonetizationModel, Product, StrategyBrief
from app.worker import enqueue

router = APIRouter(prefix="/strategy", tags=["strategy"])

SessionDep = Annotated[Session, Depends(get_session)]


def _get_brief(session: Session, product_id: int) -> StrategyBrief | None:
    return session.exec(select(StrategyBrief).where(StrategyBrief.product_id == product_id)).first()


@router.post("/{product_id}/brief", status_code=202)
def trigger_strategy_brief(product_id: int, session: SessionDep) -> dict:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    if not (product.repo_local_path or product.repo_url):
        raise HTTPException(status_code=400, detail="product has no repo to ingest")
    job = enqueue(session, "strategy_brief", product_id=product_id)
    return {"job_id": job.id, "status": job.status}


@router.post("/{product_id}/brand", status_code=202)
def trigger_brand_kit(product_id: int, session: SessionDep) -> dict:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    if _get_brief(session, product_id) is None:
        raise HTTPException(status_code=400, detail="product has no strategy brief; run it first")
    job = enqueue(session, "brand_kit", product_id=product_id)
    return {"job_id": job.id, "status": job.status}


@router.post("/{product_id}/pricing", status_code=202)
def trigger_pricing(product_id: int, session: SessionDep) -> dict:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    if product.monetization_model != MonetizationModel.CC_SUB:
        raise HTTPException(
            status_code=400,
            detail="pricing recommendation only supported for cc_sub (trial/freemium unwired)",
        )
    if _get_brief(session, product_id) is None:
        raise HTTPException(status_code=400, detail="product has no strategy brief; run it first")
    job = enqueue(session, "pricing", product_id=product_id)
    return {"job_id": job.id, "status": job.status}


# --- S1.4: review / edit / approve ----------------------------------------------------------

# The brief's JSON-bearing columns are edited as raw JSON strings (single operator); we only
# guard well-formedness so a typo can't corrupt the row the crank reads as its source of truth.
_BRIEF_JSON_FIELDS = (
    "icp_json",
    "pain_points_json",
    "channel_plan_json",
    "content_pillars_json",
    "cadence_json",
)


class BriefUpdate(BaseModel):
    positioning: str | None = None
    icp_json: str | None = None
    pain_points_json: str | None = None
    channel_plan_json: str | None = None
    content_pillars_json: str | None = None
    cadence_json: str | None = None

    @field_validator(*_BRIEF_JSON_FIELDS)
    @classmethod
    def _well_formed_json(cls, v: str | None) -> str | None:
        if v is not None:
            json.loads(v)  # raises → 422 via pydantic
        return v


@router.get("/{product_id}")
def get_strategy(product_id: int, session: SessionDep) -> StrategyBrief:
    brief = _get_brief(session, product_id)
    if brief is None:
        raise HTTPException(status_code=404, detail="product has no strategy brief")
    return brief


@router.patch("/{product_id}")
def update_strategy(product_id: int, payload: BriefUpdate, session: SessionDep) -> StrategyBrief:
    brief = _get_brief(session, product_id)
    if brief is None:
        raise HTTPException(status_code=404, detail="product has no strategy brief")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(brief, field, value)
    brief.updated_at = datetime.now(UTC)
    session.add(brief)
    session.commit()
    session.refresh(brief)
    return brief


@router.post("/{product_id}/approve")
def approve_strategy(product_id: int, session: SessionDep) -> Product:
    """Owner sign-off: complete strategy in `strategy` → `setup_ready` (the gate setup waits on)."""
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    if product.lifecycle_state != LifecycleState.STRATEGY:
        raise HTTPException(
            status_code=409,
            detail=f"product is {product.lifecycle_state}, not strategy; cannot approve",
        )
    brief = _get_brief(session, product_id)
    if brief is None:
        raise HTTPException(status_code=400, detail="product has no strategy brief")
    if not product.brand_json:
        raise HTTPException(status_code=400, detail="brand kit not generated yet")
    if (
        product.monetization_model == MonetizationModel.CC_SUB
        and product.price_amount_cents is None
    ):
        raise HTTPException(status_code=400, detail="price not set yet")

    now = datetime.now(UTC)
    brief.approved = True
    brief.approved_at = now
    brief.updated_at = now
    product.lifecycle_state = LifecycleState.SETUP_READY
    product.updated_at = now
    session.add(brief)
    session.add(product)
    session.commit()
    session.refresh(product)
    return product
