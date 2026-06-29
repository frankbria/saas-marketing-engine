"""Pricing-recommendation job handler (TECH_SPEC §5 step 4 / story S1.3).

Mirrors the S1.2 brand handler: budget pre-check → derive a cc_sub price from the product's
Marketing Brief (one Opus structured call) → fold it onto `product.price_amount_cents` +
`product.price_interval`. No new table and no lifecycle change — pricing is part of the `strategy`
phase. Only `cc_sub` is wired in v1; trial/freemium are refused (enum value only, per the AC). The
handler returns its token cost in cents; the worker adds it to `job_run.token_cost_cents` and
commits atomically.

The LLM work is injected (`generate`) so the worker wiring + persistence are testable without a
network call; the registered handler passes the real implementation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.ai.client import (
    PRICING_MAX_TOKENS,
    PRICING_MODEL,
    PricingRecommendation,
    build_client,
    recommend_pricing,
)
from app.ai.pricing import cost_cents
from app.models import MonetizationModel, Product, StrategyBrief
from app.modules.strategy.brief import month_to_date_cost_cents
from app.worker import handler

# generate(product, brief, remaining_cents) -> (recommendation, cost_cents)
# remaining_cents is the month's unspent budget (None = unlimited); generate must refuse before the
# Opus call if it can't reserve the call's worst-case cost.
GenerateFn = Callable[[Product, StrategyBrief, int | None], tuple[PricingRecommendation, int]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _real_generate(
    product: Product, brief: StrategyBrief, remaining_cents: int | None
) -> tuple[PricingRecommendation, int]:
    client = build_client()
    icp = json.loads(brief.icp_json) if brief.icp_json else {}
    icp_segment = icp.get("segment", "") if isinstance(icp, dict) else ""

    # Reserve a conservative upper bound before the (single, most expensive) Opus call so a small
    # remaining budget can't be blown past it. Count *every* input actually sent — name,
    # description, positioning, ICP segment — plus a fixed allowance for the prompt/system
    # scaffolding. ~3 chars/token is a deliberately low estimate → higher token count → higher
    # reserve → we err toward refusing.
    if remaining_cents is not None:
        PROMPT_OVERHEAD_CHARS = 600  # the fixed system + user template text around the inputs
        input_chars = (
            len(product.name)
            + len(product.description or "")
            + len(brief.positioning)
            + len(icp_segment)
            + PROMPT_OVERHEAD_CHARS
        )
        reserve = cost_cents(PRICING_MODEL, input_chars // 3, PRICING_MAX_TOKENS)
        if reserve > remaining_cents:
            raise RuntimeError(
                f"insufficient budget to reserve for pricing for product {product.id} "
                f"(need ~{reserve}, have {remaining_cents} cents)"
            )

    return recommend_pricing(
        client, product.name, product.description, brief.positioning, icp_segment
    )


def generate_product_pricing(
    job, session: Session, *, generate: GenerateFn = _real_generate
) -> int:
    """Produce + persist the cc_sub price for `job.product_id`. Returns token cost in cents."""
    if job.product_id is None:
        raise LookupError("pricing job has no product_id")
    product = session.get(Product, job.product_id)
    if product is None:
        raise LookupError(f"product {job.product_id} not found")

    # v1 recommends a price only for cc_sub; trial/freemium are enum values only (S1.3 AC). Refuse
    # rather than producing a price that nothing downstream (Stripe setup, S2.3) would use.
    if product.monetization_model != MonetizationModel.CC_SUB:
        raise RuntimeError(
            f"pricing recommendation only supports cc_sub; product {product.id} is "
            f"{product.monetization_model} (trial/freemium unwired in v1)"
        )

    # The brief is the price's grounding (S1.3 depends on S1.1). Without it there's nothing to price
    # *for* — surface rather than calling the LLM blind.
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

    rec, cost = generate(product, brief, remaining)
    product.price_amount_cents = rec.price_amount_cents
    product.price_interval = rec.price_interval
    product.updated_at = _utcnow()
    session.add(product)
    # No commit here: the worker commits the price fields + job status + token_cost_cents atomically
    # (a handler commit would let a crash before that double-spend on retry; matches S1.1/S1.2).
    return cost


# Indirection so tests can drive the full enqueue → run_due_jobs path with a stub generator
# (no network), while production uses the real LLM implementation.
_GENERATE: GenerateFn = _real_generate


@handler("pricing")
def _pricing_handler(job, session: Session) -> int:
    return generate_product_pricing(job, session, generate=_GENERATE)
