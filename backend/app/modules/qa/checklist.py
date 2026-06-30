"""Click-through QA checklist job handler (TECH_SPEC §6 / story S3.1, PRD FR-16).

Mirrors the S1.3 pricing handler: budget pre-check → one Opus structured call that produces a
concrete, ordered "open X, click Y, verify Z" checklist covering the product AND the payment
funnel → persist as `qa_checklist_item` rows. Runs while the product is in `qa` (reached after
the S2.8 launch checklist); generation does **not** change lifecycle state — recording pass/fail
and crossing `qa → live` is S3.2. Regeneration is idempotent: existing rows for the product are
replaced so a re-run never leaves a stale half-checklist behind.

The LLM work is injected (`generate`) so the worker wiring + persistence are testable without a
network call; the registered handler passes the real implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.ai.client import (
    QA_MAX_TOKENS,
    QA_MODEL,
    QaChecklist,
    build_client,
    generate_qa_checklist,
)
from app.ai.pricing import cost_cents
from app.models import LifecycleState, Product, QaChecklistItem, StrategyBrief
from app.modules.strategy.brief import month_to_date_cost_cents
from app.worker import handler

# generate(product, brief, remaining_cents) -> (checklist, cost_cents)
# remaining_cents is the month's unspent budget (None = unlimited); generate must refuse before the
# Opus call if it can't reserve the call's worst-case cost.
GenerateFn = Callable[[Product, StrategyBrief, int | None], tuple[QaChecklist, int]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _real_generate(
    product: Product, brief: StrategyBrief, remaining_cents: int | None
) -> tuple[QaChecklist, int]:
    client = build_client()

    # Reserve a conservative upper bound before the single Opus call so a small remaining budget
    # can't be blown past it. Count every input actually sent plus a fixed prompt/system allowance.
    # ~3 chars/token is a deliberately low estimate → higher token count → higher reserve → we err
    # toward refusing.
    if remaining_cents is not None:
        PROMPT_OVERHEAD_CHARS = 1200  # the fixed system + user template text around the inputs
        input_chars = (
            len(product.name)
            + len(product.description or "")
            + len(brief.positioning)
            + len(product.marketing_domain or "")
            + PROMPT_OVERHEAD_CHARS
        )
        reserve = cost_cents(QA_MODEL, input_chars // 3, QA_MAX_TOKENS)
        if reserve > remaining_cents:
            raise RuntimeError(
                f"insufficient budget to reserve for QA checklist for product {product.id} "
                f"(need ~{reserve}, have {remaining_cents} cents)"
            )

    price_label = (
        f"{product.price_amount_cents} cents / {product.price_interval}"
        if product.price_amount_cents
        else "(no price configured)"
    )
    return generate_qa_checklist(
        client,
        product.name,
        product.description,
        brief.positioning,
        product.marketing_domain,
        price_label,
    )


def generate_qa_checklist_items(
    job, session: Session, *, generate: GenerateFn = _real_generate
) -> int:
    """Produce + persist the QA checklist for `job.product_id`. Returns token cost in cents."""
    if job.product_id is None:
        raise LookupError("qa checklist job has no product_id")
    product = session.get(Product, job.product_id)
    if product is None:
        raise LookupError(f"product {job.product_id} not found")

    # The QA gate runs in `qa` (entered after the S2.8 launch checklist). Generating a tester
    # checklist before the plumbing has even reached the gate would describe a site that may not
    # exist yet — refuse rather than produce steps against unbuilt state.
    if product.lifecycle_state != LifecycleState.QA:
        raise RuntimeError(
            f"product {product.id} is {product.lifecycle_state}, not qa; "
            "the QA checklist is generated at the human QA gate"
        )

    # The brief grounds what the tester verifies (positioning, what the product does). Without it
    # there's nothing concrete to write steps for — surface rather than calling the LLM blind.
    brief = session.exec(
        select(StrategyBrief).where(StrategyBrief.product_id == product.id)
    ).first()
    if brief is None:
        raise RuntimeError(f"product {product.id} has no strategy brief; run the brief first")

    # Budget gate: 0 means unset/unlimited (onboarding default). Pre-check blocks an already-over
    # run; `remaining` lets generate refuse before the costly Opus call.
    budget = product.token_budget_cents_month
    remaining: int | None = None
    if budget > 0:
        spent = month_to_date_cost_cents(session, product.id, _utcnow())
        if spent >= budget:
            raise RuntimeError(
                f"product {product.id} over monthly token budget ({spent} >= {budget} cents)"
            )
        remaining = budget - spent

    checklist, cost = generate(product, brief, remaining)

    # Enforce the AC's coverage contract ("product login/use AND the payment funnel") before
    # persisting. A miss is a generation failure → raise so the worker retries rather than commit a
    # one-sided checklist the tester would trust as complete.
    areas = {step.area for step in checklist.steps}
    missing = {"product", "funnel"} - areas
    if missing:
        raise RuntimeError(
            f"QA checklist for product {product.id} missing coverage: {', '.join(sorted(missing))}"
        )

    # Idempotent regen: drop any prior rows so a re-run fully replaces the checklist (no stale
    # leftovers, no mixed ords). The worker commits this delete + the inserts atomically with the
    # job status, so a crash mid-handler rolls the whole thing back.
    for stale in session.exec(
        select(QaChecklistItem).where(QaChecklistItem.product_id == product.id)
    ).all():
        session.delete(stale)

    now = _utcnow()
    for ord_, step in enumerate(checklist.steps, start=1):
        session.add(
            QaChecklistItem(
                product_id=product.id,
                ord=ord_,
                instruction=step.instruction,
                blocking=step.blocking,
                updated_at=now,
            )
        )
    # No commit here: the worker commits the rows + job status + token_cost_cents atomically (a
    # handler commit would let a crash before that double-spend on retry; matches S1.1/S1.2/S1.3).
    return cost


# Indirection so tests can drive the full enqueue → run_due_jobs path with a stub generator
# (no network), while production uses the real LLM implementation.
_GENERATE: GenerateFn = _real_generate


@handler("qa_checklist")
def _qa_checklist_handler(job, session: Session) -> int:
    return generate_qa_checklist_items(job, session, generate=_GENERATE)
