"""Strategy API (private dashboard, story S1.1).

Triggering the brief enqueues a `strategy_brief` job_run; the S0.2 worker loop runs it
asynchronously (ingest + LLM can take a while). Returns 202 + the job id to poll.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.db import get_session
from app.models import Product
from app.worker import enqueue

router = APIRouter(prefix="/strategy", tags=["strategy"])

SessionDep = Annotated[Session, Depends(get_session)]


@router.post("/{product_id}/brief", status_code=202)
def trigger_strategy_brief(product_id: int, session: SessionDep) -> dict:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    if not (product.repo_local_path or product.repo_url):
        raise HTTPException(status_code=400, detail="product has no repo to ingest")
    job = enqueue(session, "strategy_brief", product_id=product_id)
    return {"job_id": job.id, "status": job.status}
