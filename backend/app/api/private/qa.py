"""QA API (private dashboard, stories S2.7 + S2.8).

Both gate steps run synchronously on demand — they spend no LLM tokens and make no real network
calls, so each returns immediately rather than going through the job queue. The gate to `qa` is
two-step (TECH_SPEC line 112: smoke pass **+** checklist emitted):

1. `POST /smoke-test` (S2.7) — records the pre-QA smoke verdict on `smoke_test_json`. A failure
   keeps the product in `setup_done` so broken plumbing never reaches the human QA gate (§6.7). It
   does **not** transition state on its own.
2. `POST /launch-checklist` (S2.8) — requires a *passed* smoke test, emits the launch checklist from
   real setup output onto `launch_checklist_json`, and crosses `setup_done → qa`.

Both results are folded onto the product so the dashboard reads them from the existing product GET.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.db import get_session
from app.models import LifecycleState, Product
from app.modules.qa.launch_checklist import LaunchChecklist, emit_launch_checklist
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
    # Record the verdict only — emitting the launch checklist (S2.8) is what crosses to `qa`, so the
    # gate requires smoke pass *and* checklist emitted (TECH_SPEC line 112). A failure is still
    # persisted for the dashboard.
    product.smoke_test_json = result.model_dump_json()
    product.updated_at = datetime.now(UTC)
    session.add(product)
    session.commit()
    return result


@router.post("/{product_id}/launch-checklist")
def emit_checklist(product_id: int, session: SessionDep) -> LaunchChecklist:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    if product.lifecycle_state != LifecycleState.SETUP_DONE:
        raise HTTPException(
            status_code=409,
            detail=f"product is {product.lifecycle_state}, not setup_done; "
            "the launch checklist is emitted after setup completes",
        )
    smoke = (
        SmokeTestResult.model_validate_json(product.smoke_test_json)
        if product.smoke_test_json
        else None
    )
    if smoke is None or not smoke.passed:
        raise HTTPException(
            status_code=409,
            detail="run and pass the pre-QA smoke test before emitting the launch checklist",
        )

    checklist = emit_launch_checklist(product, session)
    # Re-check the gate after the (synchronous) read so two overlapping POSTs can't both start from
    # setup_done and double-cross the gate.
    session.refresh(product)
    if product.lifecycle_state != LifecycleState.SETUP_DONE:
        raise HTTPException(
            status_code=409,
            detail="product state changed while the checklist was emitted; retry from latest state",
        )
    product.launch_checklist_json = checklist.model_dump_json()
    product.updated_at = datetime.now(UTC)
    product.lifecycle_state = LifecycleState.QA
    session.add(product)
    session.commit()
    return checklist
