"""QA API (private dashboard, story S2.7).

The pre-QA smoke test runs synchronously on demand — it spends no LLM tokens and makes no real
network calls, so it returns a verdict immediately rather than going through the job queue. A full
pass advances the product `setup_done → qa`; any stage failure keeps it in `setup_done`, so broken
plumbing never reaches the human QA gate (TECH_SPEC §6.7). The result is folded onto the product
(`smoke_test_json`) so the dashboard surfaces it from the existing product GET.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.db import get_session
from app.models import LifecycleState, Product
from app.modules.qa.smoke_test import SmokeTestResult, run_smoke_test

router = APIRouter(prefix="/qa", tags=["qa"])

SessionDep = Annotated[Session, Depends(get_session)]


@router.post("/{product_id}/smoke-test")
def run_smoke(product_id: int, session: SessionDep) -> SmokeTestResult:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    if product.lifecycle_state != LifecycleState.SETUP_DONE:
        raise HTTPException(
            status_code=409,
            detail=f"product is {product.lifecycle_state}, not setup_done; "
            "the smoke test runs after setup completes",
        )

    result = run_smoke_test(product)
    # The gate was checked before the (synchronous) run; re-check after so two overlapping POSTs
    # can't both start from setup_done and have the slower one overwrite the verdict.
    session.refresh(product)
    if product.lifecycle_state != LifecycleState.SETUP_DONE:
        raise HTTPException(
            status_code=409,
            detail="product state changed while the smoke test ran; retry from the latest state",
        )
    product.smoke_test_json = result.model_dump_json()
    product.updated_at = datetime.now(UTC)
    if result.passed:
        # Only a full pass crosses the gate; a failure leaves the product in setup_done.
        product.lifecycle_state = LifecycleState.QA
    session.add(product)
    session.commit()
    return result
