"""Pre-QA funnel smoke test (S2.7 / TECH_SPEC §6.7).

Before a product can reach the human QA gate it must pass an automated smoke test that asserts the
generated site **builds**, the four-stage funnel path (`impression/visit/signup/paid`) **fires**,
and Checkout hits the **correct test price**. A failure keeps the product in `setup_done`; only a
full pass advances it to `qa` (the transition is owned by the route in `api/private/qa.py`).

The funnel is exercised against an **isolated in-memory SQLite database seeded with a clone of the
product**, so synthetic smoke traffic never pollutes the product's real funnel/revenue metrics. The
real funnel route functions are called directly (`record_visit`/`record_lead`/`start_checkout` +
the webhook's `_attribute_paid_metric`) — no HTTP server / event-loop portal needed. The
`build`/`impression` stages assert the *already-built* artifact in the product workspace; re-running
the build would spend LLM tokens, so the smoke test verifies the build's output, not a rebuild.

ponytail: `impression` has no backend plumbing in v1 (channel reach is S4.x) — the stage verifies
the site's impression→visit entry hook is wired (the on-load `visit` beacon). Full impression
metrics arrive with the channel adapters.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import BackgroundTasks, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.public import funnel as funnel_api
from app.api.public.funnel import CheckoutCreate, LeadCreate, VisitCreate
from app.api.public.stripe import _attribute_paid_metric
from app.models import (
    FunnelEvent,
    FunnelEventType,
    MetricEvent,
    MetricStage,
    Product,
)
from app.workspace import workspace_path

# Synthetic attribution token threaded through visit → lead → checkout → paid in the isolated DB.
_TOKEN = "smoke-test-token"

# The funnel-contract calls the template's vanilla JS must wire (see site-template/index.html.j2).
_FUNNEL_HOOKS = ('post("/visit"', 'post("/lead"', 'post("/checkout"')
# The impression→visit entry hook: the `visit` beacon fired on page load.
_VISIT_BEACON = 'post("/visit", payload());'


class StageResult(BaseModel):
    stage: str  # build | impression | visit | signup | checkout | paid
    ok: bool
    detail: str = ""


class SmokeTestResult(BaseModel):
    passed: bool
    ran_at: datetime
    stages: list[StageResult]


def _completed_event(token: str, product_id: int, amount_cents: int) -> dict:
    """A minimal Stripe `checkout.session.completed` event shaped like the real webhook payload."""
    return {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_smoke",
                "client_reference_id": token,
                "amount_total": amount_cents,
                "metadata": {"product_id": str(product_id)},
            }
        },
    }


def run_smoke_test(product: Product) -> SmokeTestResult:
    """Run all six smoke-test stages for `product`; never raises — every stage reports pass/fail."""
    stages: list[StageResult] = []

    # --- build + impression: assert the real built artifact (no rebuild, no token spend) ---
    index = workspace_path(product.slug) / "site" / "index.html"
    # An unreadable artifact (permissions, deleted between checks, bad UTF-8) is a stage failure,
    # not a 500 — `run_smoke_test` never raises.
    html = ""
    read_error = ""
    try:
        if index.exists():
            html = index.read_text(encoding="utf-8")
    except OSError as exc:
        read_error = str(exc)
    except UnicodeDecodeError:
        read_error = "built index.html is not valid UTF-8"

    if read_error:
        stages.append(StageResult(stage="build", ok=False, detail=f"unreadable site: {read_error}"))
    elif not index.exists():
        stages.append(StageResult(stage="build", ok=False, detail=f"no built site at {index}"))
    else:
        stages.append(
            StageResult(
                stage="build",
                ok=bool(html.strip()),
                detail="" if html.strip() else "built index.html is empty",
            )
        )

    missing = [hook for hook in _FUNNEL_HOOKS if hook not in html]
    if read_error:
        imp_detail = f"site artifact unreadable: {read_error}"
    elif not html:
        imp_detail = "no site artifact to inspect"
    elif missing:
        imp_detail = f"missing funnel hooks: {', '.join(missing)}"
    elif _VISIT_BEACON not in html:
        imp_detail = "visit beacon not fired on page load"
    else:
        imp_detail = ""
    stages.append(StageResult(stage="impression", ok=not imp_detail, detail=imp_detail))

    # --- visit / signup / checkout / paid: exercise the funnel against an isolated in-memory DB so
    #     synthetic traffic never lands in the product's real funnel/revenue tables ---
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as s:
            clone = Product(
                name=product.name,
                slug=product.slug,
                monetization_model=product.monetization_model,
                price_amount_cents=product.price_amount_cents,
                price_interval=product.price_interval,
                stripe_price_id=product.stripe_price_id,
                marketing_domain=product.marketing_domain,
            )
            s.add(clone)
            s.commit()
            s.refresh(clone)
            slug = clone.slug
            pid = clone.id
            # None is a failure (no configured price), not a synthetic $0 — see the paid stage.
            price = clone.price_amount_cents

            # visit
            try:
                funnel_api.record_visit(
                    slug, VisitCreate(first_touch_token=_TOKEN, utm_source="smoke"), s
                )
                n = len(
                    s.exec(
                        select(FunnelEvent).where(FunnelEvent.event_type == FunnelEventType.VISIT)
                    ).all()
                )
                stages.append(
                    StageResult(
                        stage="visit", ok=n == 1, detail="" if n == 1 else f"{n} visit rows"
                    )
                )
            except Exception as exc:  # noqa: BLE001 — a stage failure is data, not a crash
                stages.append(StageResult(stage="visit", ok=False, detail=str(exc)))

            # signup (lead) — send seam stubbed; the welcome email is S2.4's concern here
            try:
                funnel_api.record_lead(
                    slug,
                    LeadCreate(email="smoke@smoke.test", first_touch_token=_TOKEN),
                    s,
                    BackgroundTasks(),
                    lambda *a, **k: None,
                )
                n = len(
                    s.exec(
                        select(FunnelEvent).where(FunnelEvent.event_type == FunnelEventType.LEAD)
                    ).all()
                )
                stages.append(
                    StageResult(
                        stage="signup", ok=n == 1, detail="" if n == 1 else f"{n} lead rows"
                    )
                )
            except Exception as exc:  # noqa: BLE001
                stages.append(StageResult(stage="signup", ok=False, detail=str(exc)))

            # checkout — must hit the correct test price (the product's stripe_price_id)
            captured: dict = {}

            def _capture(**kwargs) -> str:
                captured.update(kwargs)
                return "https://checkout.stripe.com/c/pay/cs_smoke"

            try:
                funnel_api.start_checkout(
                    slug,
                    CheckoutCreate(client_reference_id=_TOKEN, first_touch_token=_TOKEN),
                    s,
                    _capture,
                )
                ok = (
                    bool(clone.stripe_price_id)
                    and captured.get("price_id") == clone.stripe_price_id
                )
                detail = (
                    ""
                    if ok
                    else f"checkout price_id={captured.get('price_id')!r} != "
                    f"stripe_price_id={clone.stripe_price_id!r}"
                )
                stages.append(StageResult(stage="checkout", ok=ok, detail=detail))
            except HTTPException as exc:
                stages.append(
                    StageResult(
                        stage="checkout", ok=False, detail=f"{exc.status_code}: {exc.detail}"
                    )
                )
            except Exception as exc:  # noqa: BLE001
                stages.append(StageResult(stage="checkout", ok=False, detail=str(exc)))

            # paid — the attribution webhook closes the chain at the test price. A product with no
            # configured price can't have a "correct" paid amount — fail rather than fake a $0 sale.
            if price is None:
                stages.append(
                    StageResult(stage="paid", ok=False, detail="product has no price_amount_cents")
                )
            else:
                try:
                    _attribute_paid_metric(_completed_event(_TOKEN, pid, price), s)
                    rows = s.exec(
                        select(MetricEvent).where(MetricEvent.stage == MetricStage.PAID)
                    ).all()
                    ok = len(rows) == 1 and rows[0].product_id == pid and rows[0].value == price
                    if len(rows) != 1:
                        detail = f"{len(rows)} paid metric rows"
                    elif rows[0].product_id != pid:
                        detail = f"paid metric attributed to {rows[0].product_id}, not {pid}"
                    elif rows[0].value != price:
                        detail = f"paid value {rows[0].value} != test price {price}"
                    else:
                        detail = ""
                    stages.append(StageResult(stage="paid", ok=ok, detail=detail))
                except Exception as exc:  # noqa: BLE001
                    stages.append(StageResult(stage="paid", ok=False, detail=str(exc)))
    finally:
        engine.dispose()

    return SmokeTestResult(
        passed=all(stage.ok for stage in stages),
        ran_at=datetime.now(UTC),
        stages=stages,
    )
