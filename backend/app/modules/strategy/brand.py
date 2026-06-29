"""Brand-kit job handler (TECH_SPEC §5 step 3 / story S1.2).

Mirrors the S1.1 brief handler: budget pre-check → derive a brand kit from the product's
Marketing Brief (one Opus structured call) → fold it onto `product.brand_json`. No new table
and no lifecycle change — the brand kit is part of the `strategy` phase. The handler returns its
token cost in cents; the worker adds it to `job_run.token_cost_cents` and commits atomically.

The LLM work is injected (`generate`) so the worker wiring + persistence are testable without a
network call; the registered handler passes the real implementation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.ai.client import (
    BRAND_MAX_TOKENS,
    BRAND_MODEL,
    BrandKit,
    build_client,
    generate_brand_kit,
)
from app.ai.pricing import cost_cents
from app.models import Product, StrategyBrief
from app.modules.strategy.brief import month_to_date_cost_cents
from app.worker import handler

# generate(product, brief, remaining_cents) -> (kit, cost_cents)
# remaining_cents is the month's unspent budget (None = unlimited); generate must refuse before the
# Opus call if it can't reserve the call's worst-case cost.
GenerateFn = Callable[[Product, StrategyBrief, int | None], tuple[BrandKit, int]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _real_generate(
    product: Product, brief: StrategyBrief, remaining_cents: int | None
) -> tuple[BrandKit, int]:
    client = build_client()
    content_pillars = json.loads(brief.content_pillars_json)

    # Reserve a conservative upper bound before the (single, most expensive) Opus call so a small
    # remaining budget can't be blown past it. Count *every* input actually sent — name,
    # description, positioning, pillars — plus a fixed allowance for the prompt/system scaffolding.
    # ~3 chars/token is a deliberately low estimate → higher token count → higher reserve → we err
    # toward refusing.
    if remaining_cents is not None:
        PROMPT_OVERHEAD_CHARS = 600  # the fixed system + user template text around the inputs
        input_chars = (
            len(product.name)
            + len(product.description or "")
            + len(brief.positioning)
            + len(brief.content_pillars_json)
            + PROMPT_OVERHEAD_CHARS
        )
        reserve = cost_cents(BRAND_MODEL, input_chars // 3, BRAND_MAX_TOKENS)
        if reserve > remaining_cents:
            raise RuntimeError(
                f"insufficient budget to reserve for brand kit for product {product.id} "
                f"(need ~{reserve}, have {remaining_cents} cents)"
            )

    return generate_brand_kit(
        client, product.name, product.description, brief.positioning, content_pillars
    )


def generate_product_brand_kit(
    job, session: Session, *, generate: GenerateFn = _real_generate
) -> int:
    """Produce + persist the brand kit for `job.product_id`. Returns token cost in cents."""
    if job.product_id is None:
        raise LookupError("brand_kit job has no product_id")
    product = session.get(Product, job.product_id)
    if product is None:
        raise LookupError(f"product {job.product_id} not found")

    # The brief is the brand kit's grounding (S1.2 depends on S1.1). Without it there's nothing to
    # be on-brand *for* — surface rather than calling the LLM blind.
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

    kit, cost = generate(product, brief, remaining)
    product.brand_json = kit.model_dump_json()
    product.updated_at = _utcnow()
    session.add(product)
    # No commit here: the worker commits the brand_json + job status + token_cost_cents atomically
    # (a handler commit would let a crash before that double-spend on retry; matches S1.1).
    return cost


# Indirection so tests can drive the full enqueue → run_due_jobs path with a stub generator
# (no network), while production uses the real LLM implementation.
_GENERATE: GenerateFn = _real_generate


@handler("brand_kit")
def _brand_kit_handler(job, session: Session) -> int:
    return generate_product_brand_kit(job, session, generate=_GENERATE)
