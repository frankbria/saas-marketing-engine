"""Setup API (private dashboard, story S2.1+).

Triggering the site build enqueues a `setup_site` job_run; the worker generates the on-brand copy,
renders the template, statically exports it to the product workspace, and deploys it under
`marketing_domain`. Setup begins once the owner has approved the strategy (`setup_ready`).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.db import get_session
from app.models import LifecycleState, MonetizationModel, Product
from app.modules.setup.stripe_setup import STRIPE_INTERVALS
from app.worker import enqueue

router = APIRouter(prefix="/setup", tags=["setup"])

SessionDep = Annotated[Session, Depends(get_session)]


@router.post("/{product_id}/site", status_code=202)
def trigger_site_build(product_id: int, session: SessionDep) -> dict:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    if product.lifecycle_state != LifecycleState.SETUP_READY:
        raise HTTPException(
            status_code=409,
            detail=f"product is {product.lifecycle_state}, not setup_ready; approve strategy first",
        )
    if not product.brand_json:
        raise HTTPException(status_code=400, detail="brand kit not generated yet")
    job = enqueue(session, "setup_site", product_id=product_id)
    return {"job_id": job.id, "status": job.status}


@router.post("/{product_id}/stripe", status_code=202)
def trigger_stripe_setup(product_id: int, session: SessionDep) -> dict:
    """Enqueue Stripe product+price creation (S2.3); the worker persists `stripe_price_id`."""
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    # Setup begins only once the owner has approved the strategy — don't create paid external Stripe
    # resources for an unapproved product (mirrors the site-build gate above).
    if product.lifecycle_state != LifecycleState.SETUP_READY:
        raise HTTPException(
            status_code=409,
            detail=f"product is {product.lifecycle_state}, not setup_ready; approve strategy first",
        )
    if product.monetization_model != MonetizationModel.CC_SUB:
        raise HTTPException(
            status_code=400,
            detail=f"stripe setup only supports cc_sub; product is {product.monetization_model}",
        )
    if product.price_amount_cents is None or product.price_interval is None:
        raise HTTPException(
            status_code=400, detail="no price set; run the pricing recommendation first"
        )
    if product.price_interval not in STRIPE_INTERVALS:
        raise HTTPException(
            status_code=400,
            detail=f"price_interval {product.price_interval!r} not supported (use month/year)",
        )
    job = enqueue(session, "stripe_setup", product_id=product_id)
    return {"job_id": job.id, "status": job.status}
