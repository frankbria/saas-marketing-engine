"""Setup API (private dashboard, story S2.1+).

Triggering the site build enqueues a `setup_site` job_run; the worker generates the on-brand copy,
renders the template, statically exports it to the product workspace, and deploys it under
`marketing_domain`. Setup begins once the owner has approved the strategy (`setup_ready`).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.db import get_session
from app.models import LifecycleState, Product
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
