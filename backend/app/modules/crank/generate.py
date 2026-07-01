"""Generate-step job handler (TECH_SPEC §8.2 / story S4.2).

Fills the `@handler("generate")` seam S4.1's fan-out left: for one (product, channel, content_type)
cell, produce one on-brand `content_item` at the `generated` state. The generate → critic (S4.3) →
guard (S4.4) → publish (S4.5) pipeline advances the same row's `status` in place; S4.2 is just the
first step.

Mirrors the S1.1/S1.2 handler pattern: budget pre-check + reservation before the LLM call, the LLM
work injected (`generate=`) so the worker wiring + persistence are testable without a network call,
and no commit here — the worker commits the new row atomically with the job's DONE status + cost.

**Novelty (AC):** recent items already on the channel are fetched and fed into the generator prompt
so it avoids near-duplicates. Pre-S4.5 nothing is `published` yet, so "recent" is the most-recent
items in any non-terminal-failure state; this narrows to published content once S4.5 lands.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import Session, col, select

from app.ai.client import (
    GEN_BLOG_MAX_TOKENS,
    GEN_MODEL,
    GEN_SOCIAL_MAX_TOKENS,
    BrandKit,
    build_client,
    generate_blog_article,
    generate_social_post,
)
from app.ai.pricing import cost_cents
from app.models import ContentItem, Product, StrategyBrief
from app.models.content_item import _TERMINAL_FAILURE, ContentItemStatus
from app.modules.crank.crank import ContentType
from app.modules.strategy.brief import month_to_date_cost_cents
from app.worker import handler

RECENT_LIMIT = 5  # how many recent items to feed the generator for novelty
_RECENT_BODY_CHARS = 500  # cap each recent item's text so the novelty block stays bounded


@dataclass
class Generated:
    """Normalized generator output the handler persists as a ContentItem (decouples persistence
    from the per-type LLM schema, so a stub can drive the full worker path)."""

    body: str
    meta: dict  # serialized to content_item.meta_json (referenced pillar + per-type metadata)
    title: str | None = None  # blog headline; None for social


# generate(product, brief, brand_kit, content_type, recent_items) -> (Generated, cost_cents)
GenerateFn = Callable[[Product, StrategyBrief, BrandKit, str, list[str]], tuple[Generated, int]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _recent_items(session: Session, product_id: int, channel_id: int) -> list[str]:
    """Most-recent non-failed items for the channel, as short text for the novelty prompt."""
    rows = session.exec(
        select(ContentItem)
        .where(
            ContentItem.product_id == product_id,
            ContentItem.channel_id == channel_id,
            col(ContentItem.status).notin_(_TERMINAL_FAILURE),
        )
        .order_by(col(ContentItem.created_at).desc())
        .limit(RECENT_LIMIT)
    ).all()
    items: list[str] = []
    for row in rows:
        text = row.body[:_RECENT_BODY_CHARS]
        items.append(f"{row.title} — {text}" if row.title else text)
    return items


def _real_generate(
    product: Product,
    brief: StrategyBrief,
    brand_kit: BrandKit,
    content_type: str,
    recent_items: list[str],
) -> tuple[Generated, int]:
    pillars = json.loads(brief.content_pillars_json)

    # Validate the content type before building the client: an unknown type is a wiring bug that
    # must fail the same way with or without an API key (video/podcast are Phase B; the crank
    # fan-out never emits them in Phase A).
    if content_type == ContentType.SOCIAL.value:
        post, cost = generate_social_post(
            build_client(), product.name, brand_kit, brief.positioning, pillars, recent_items
        )
        meta = {"pillar": post.pillar, "hashtags": post.hashtags}
        return Generated(body=post.body, meta=meta), cost

    if content_type == ContentType.BLOG.value:
        article, cost = generate_blog_article(
            build_client(), product.name, brand_kit, brief.positioning, pillars, recent_items
        )
        meta = {
            "pillar": article.pillar,
            "slug": article.slug,
            "meta_description": article.meta_description,
        }
        return Generated(title=article.title, body=article.body, meta=meta), cost

    raise LookupError(f"no generator for content_type {content_type!r} (Phase A is social|blog)")


def _reservation_input_estimate(
    brief: StrategyBrief, brand_json: str, recent_items: list[str]
) -> int:
    """Rough input-token estimate for the budget reservation. ~3 chars/token, deliberately low
    (→ higher token count → higher reserve → err toward refusing), matching brief.py."""
    chars = len(brief.positioning) + len(brief.content_pillars_json) + len(brand_json)
    chars += sum(len(item) for item in recent_items)
    return chars // 3


def run_generate(job, session: Session, *, generate: GenerateFn = _real_generate) -> int:
    """Produce + persist one content item for the fanned-out cell. Returns token cost in cents."""
    if job.product_id is None or job.channel_id is None or job.content_type is None:
        raise LookupError(
            f"generate job {job.id} missing product_id/channel_id/content_type "
            "(should be set by the crank fan-out)"
        )

    product = session.get(Product, job.product_id)
    if product is None:
        raise LookupError(f"product {job.product_id} not found")
    if product.brand_json is None:
        raise LookupError(f"product {product.id} has no brand_json (brand kit not generated)")
    brief = session.exec(
        select(StrategyBrief).where(StrategyBrief.product_id == product.id)
    ).first()
    if brief is None:
        raise LookupError(f"product {product.id} has no strategy brief")

    brand_kit = BrandKit.model_validate_json(product.brand_json)
    recent_items = _recent_items(session, product.id, job.channel_id)

    # Budget gate (mirrors brief.py): 0 = unset/unlimited. Pre-check blocks a run already over;
    # then reserve the call's worst-case cost so a small remaining budget can't be blown past.
    budget = product.token_budget_cents_month
    if budget > 0:
        spent = month_to_date_cost_cents(session, product.id, _utcnow())
        if spent >= budget:
            raise RuntimeError(
                f"product {product.id} over monthly token budget ({spent} >= {budget} cents)"
            )
        remaining = budget - spent
        max_out = (
            GEN_BLOG_MAX_TOKENS
            if job.content_type == ContentType.BLOG.value
            else GEN_SOCIAL_MAX_TOKENS
        )
        est_input = _reservation_input_estimate(brief, product.brand_json, recent_items)
        reserve = cost_cents(GEN_MODEL, est_input, max_out)
        if reserve > remaining:
            raise RuntimeError(
                f"insufficient budget to reserve for generation for product {product.id} "
                f"(need ~{reserve}, have {remaining} cents)"
            )

    gen, cost = generate(product, brief, brand_kit, job.content_type, recent_items)

    session.add(
        ContentItem(
            product_id=product.id,
            channel_id=job.channel_id,
            content_type=job.content_type,
            status=ContentItemStatus.GENERATED,
            title=gen.title,
            body=gen.body,
            meta_json=json.dumps(gen.meta),
        )
    )
    # No commit here: the worker commits the content item atomically with the job's DONE status +
    # token_cost_cents (committing early would let a crash requeue the job and double-spend).
    return cost


# Indirection so tests can drive the full enqueue → run_due_jobs path with a stub generator
# (no network), while production uses the real LLM implementation.
_GENERATE: GenerateFn = _real_generate


@handler("generate")
def _generate_handler(job, session: Session) -> int:
    return run_generate(job, session, generate=_GENERATE)
