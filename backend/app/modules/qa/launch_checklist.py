"""Launch checklist emission (S2.8 / TECH_SPEC §6, PRD FR-15).

After a product passes the pre-QA smoke test (S2.7), the engine emits a **launch checklist** from
its real setup output so the human QA gate has something concrete to verify. The checklist is purely
deterministic — read from existing state (the stored smoke verdict, channel rows, the human-setup
punch-list, Stripe config), no LLM tokens and no network, so it returns synchronously like the smoke
test. Emitting it is what crosses `setup_done → qa` (the route owns the transition; see
`api/private/qa.py`); incomplete human-setup items are *surfaced* here, not blocking — the smoke
pass is the hard gate.

This is the pre-launch readiness snapshot, distinct from the AI click-through QA checklist
(`qa_checklist_item`, S3.1/S3.2) the tester marks pass/fail.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel
from sqlmodel import Session, select

from app.models import Channel, Product, SetupChecklistItem, SetupItemStatus
from app.modules.qa.smoke_test import SmokeTestResult

# Funnel-contract stages (S2.7) the launch checklist rolls up into one "wired" line.
_FUNNEL_STAGES = ("impression", "visit", "signup", "checkout", "paid")


class LaunchChecklistItem(BaseModel):
    ord: int
    label: str
    detail: str = ""
    ready: bool


class LaunchChecklist(BaseModel):
    emitted_at: datetime
    items: list[LaunchChecklistItem]


def emit_launch_checklist(
    product: Product, session: Session, smoke: SmokeTestResult | None
) -> LaunchChecklist:
    """Build the launch checklist from `product`'s real setup output and the (already-validated)
    smoke verdict. Pure aside from two read-only queries; it does not parse stored JSON, so the
    caller owns validating `smoke_test_json`."""
    stage_ok = {s.stage: s.ok for s in smoke.stages} if smoke else {}

    items: list[LaunchChecklistItem] = []

    items.append(
        LaunchChecklistItem(
            ord=1,
            label="Site built & deployed",
            ready=stage_ok.get("build", False),
            detail="" if stage_ok.get("build") else "smoke-test build stage not passed",
        )
    )

    funnel_ready = all(stage_ok.get(s, False) for s in _FUNNEL_STAGES)
    missing = [s for s in _FUNNEL_STAGES if not stage_ok.get(s, False)]
    items.append(
        LaunchChecklistItem(
            ord=2,
            label="Funnel contract wired (impression→visit→signup→checkout→paid)",
            ready=funnel_ready,
            detail="" if funnel_ready else f"stages not passing: {', '.join(missing)}",
        )
    )

    stripe_ready = bool(product.stripe_price_id and product.price_amount_cents)
    items.append(
        LaunchChecklistItem(
            ord=3,
            label="Stripe test-mode checkout configured",
            ready=stripe_ready,
            detail="" if stripe_ready else "missing stripe_price_id or price_amount_cents",
        )
    )

    items.append(
        LaunchChecklistItem(
            ord=4,
            label="Pre-QA smoke test passed",
            ready=bool(smoke and smoke.passed),
            detail="" if smoke and smoke.passed else "smoke test missing or failed",
        )
    )

    channels = session.exec(
        select(Channel).where(Channel.product_id == product.id, Channel.enabled)
    ).all()
    items.append(
        LaunchChecklistItem(
            ord=5,
            label="Channels prepared",
            ready=len(channels) > 0,
            detail=", ".join(c.type for c in channels) or "no channels prepared",
        )
    )

    setup_items = session.exec(
        select(SetupChecklistItem).where(SetupChecklistItem.product_id == product.id)
    ).all()
    pending = [i for i in setup_items if i.status != SetupItemStatus.DONE]
    done = len(setup_items) - len(pending)
    items.append(
        LaunchChecklistItem(
            ord=6,
            label=f"Human setup steps complete ({done}/{len(setup_items)})",
            ready=len(pending) == 0,
            detail=(
                "" if not pending else "pending: " + "; ".join(i.instruction for i in pending[:5])
            ),
        )
    )

    return LaunchChecklist(emitted_at=datetime.now(UTC), items=items)
