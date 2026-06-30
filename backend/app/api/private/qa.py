"""QA API (private dashboard, stories S2.7 + S2.8 + S3.1 + S3.2).

Both gate steps run synchronously on demand — they spend no LLM tokens and make no real network
calls, so each returns immediately rather than going through the job queue. The gate to `qa` is
two-step (TECH_SPEC line 112: smoke pass **+** checklist emitted):

1. `POST /smoke-test` (S2.7) — records the pre-QA smoke verdict on `smoke_test_json`. A failure
   keeps the product in `setup_done` so broken plumbing never reaches the human QA gate (§6.7). It
   does **not** transition state on its own.
2. `POST /launch-checklist` (S2.8) — requires a *passed* smoke test, emits the launch checklist from
   real setup output onto `launch_checklist_json`, and crosses `setup_done → qa`.

Both results are folded onto the product so the dashboard reads them from the existing product GET.

3. `POST /checklist` (S3.1) — at the QA gate (`qa` state), enqueues a job that generates the
   click-through QA checklist (one Opus call, async like strategy) as `qa_checklist_item` rows;
   `GET /checklist` lists them.
4. `PATCH /checklist/{item_id}` + `POST /go-live` (S3.2) — the tester marks each item pass/fail with
   a comment; go-live is blocked until every *blocking* item passes, then crosses `qa → live`.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlmodel import Session, select

from app.db import get_session
from app.models import LifecycleState, Product, QaChecklistItem, QaItemStatus
from app.modules.qa.launch_checklist import LaunchChecklist, emit_launch_checklist
from app.modules.qa.smoke_test import SmokeTestResult, run_smoke_test
from app.worker import enqueue

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
    if not product.smoke_test_json:
        raise HTTPException(
            status_code=409,
            detail="run and pass the pre-QA smoke test before emitting the launch checklist",
        )
    try:
        smoke = SmokeTestResult.model_validate_json(product.smoke_test_json)
    except ValidationError as exc:
        # Stored verdict is corrupt/schema-drifted — don't 500; tell the operator to re-run it.
        raise HTTPException(
            status_code=409,
            detail="stored smoke-test result is unreadable; re-run the pre-QA smoke test",
        ) from exc
    if not smoke.passed:
        raise HTTPException(
            status_code=409,
            detail="run and pass the pre-QA smoke test before emitting the launch checklist",
        )

    checklist = emit_launch_checklist(product, session, smoke)
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


@router.post("/{product_id}/checklist", status_code=202)
def trigger_qa_checklist(product_id: int, session: SessionDep) -> dict:
    """Enqueue click-through QA checklist generation (S3.1). 202 + job id to poll.

    Gated to `qa`: the checklist describes the built product + funnel a tester clicks through, so
    it's only meaningful once the launch checklist (S2.8) has crossed the product into the QA gate.
    """
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    if product.lifecycle_state != LifecycleState.QA:
        raise HTTPException(
            status_code=409,
            detail=f"product is {product.lifecycle_state}, not qa; "
            "emit the launch checklist to reach the QA gate first",
        )
    job = enqueue(session, "qa_checklist", product_id=product_id)
    return {"job_id": job.id, "status": job.status}


@router.get("/{product_id}/checklist")
def get_qa_checklist(product_id: int, session: SessionDep) -> list[QaChecklistItem]:
    """List the product's QA checklist items in order (S3.1; pass/fail surface is S3.2)."""
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    return session.exec(
        select(QaChecklistItem)
        .where(QaChecklistItem.product_id == product_id)
        .order_by(QaChecklistItem.ord)
    ).all()


class QaItemUpdate(BaseModel):
    # A tester records a verdict — `pass` or `fail`. `pending` is the generated default, not
    # something the gate lets you set back, so it's excluded from the contract.
    status: Literal[QaItemStatus.PASS, QaItemStatus.FAIL]
    comment: str | None = None


@router.patch("/{product_id}/checklist/{item_id}")
def mark_qa_item(
    product_id: int, item_id: int, payload: QaItemUpdate, session: SessionDep
) -> QaChecklistItem:
    """Record a tester's pass/fail + optional comment on one QA item (S3.2).

    Gated to `qa`: the human QA gate is the only point where marking items is meaningful (a product
    already `live` has cleared the gate; one still in setup hasn't reached it).
    """
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    if product.lifecycle_state != LifecycleState.QA:
        raise HTTPException(
            status_code=409,
            detail=f"product is {product.lifecycle_state}, not qa; "
            "items are marked at the human QA gate",
        )
    item = session.get(QaChecklistItem, item_id)
    if item is None or item.product_id != product_id:
        raise HTTPException(status_code=404, detail="checklist item not found for this product")
    item.status = payload.status
    item.comment = payload.comment
    item.updated_at = datetime.now(UTC)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@router.post("/{product_id}/go-live")
def go_live(product_id: int, session: SessionDep) -> Product:
    """Cross `qa → live` once every *blocking* QA item passes (S3.2).

    Blocked (409) unless the checklist has been generated *and* every blocking item is `pass`;
    non-blocking items never block. Both the product state and the blocking-item verdicts are
    re-validated together right before the write, so a concurrent `PATCH` that flips a blocking item
    can't slip a launch through the gate.
    """
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")

    def _check_gate() -> None:
        if product.lifecycle_state != LifecycleState.QA:
            raise HTTPException(
                status_code=409,
                detail=f"product is {product.lifecycle_state}, not qa; "
                "reach the QA gate before going live",
            )
        items = session.exec(
            select(QaChecklistItem)
            .where(QaChecklistItem.product_id == product_id)
            .order_by(QaChecklistItem.ord)
        ).all()
        if not items:
            raise HTTPException(
                status_code=409,
                detail="no QA checklist for this product; "
                "generate it and pass it before going live",
            )
        unpassed = [i.ord for i in items if i.blocking and i.status != QaItemStatus.PASS]
        if unpassed:
            raise HTTPException(
                status_code=409,
                detail="blocking QA items not passed: " + ", ".join(str(o) for o in unpassed),
            )

    _check_gate()
    # Re-read product + items and re-validate the full gate right before the write, so a `PATCH`
    # that flipped a blocking item (or a state change) between the first check and the commit is
    # caught rather than overrun.
    session.expire_all()
    _check_gate()
    product.lifecycle_state = LifecycleState.LIVE
    product.updated_at = datetime.now(UTC)
    session.add(product)
    session.commit()
    session.refresh(product)
    return product
