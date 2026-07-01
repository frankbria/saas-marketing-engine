"""Generate + critic-gate job handler (TECH_SPEC §8.2 / stories S4.2 + S4.3).

Fills the `@handler("generate")` seam S4.1's fan-out left: for one (product, channel, content_type)
cell, produce one on-brand `content_item` and run it through the critic+safety gate. The remaining
pipeline — deterministic guard (S4.4), publish (S4.5) — advances the same row's `status` in place.

Per §8.2 this is a single per-item flow, so the critic lives here rather than in a separate handler:
"regenerate (max N)" simply re-invokes the generator. Each attempt is generate → one critic+safety
call; a safety failure hard-blocks (`guard_failed`), a passing score accepts (`critic_passed`), a
low score regenerates, and exhausting the attempts skips+logs (`critic_failed`).

Mirrors the S1.1/S1.2 handler pattern: budget pre-check + per-attempt reservation before each LLM
pass, the LLM work injected (`generate=`/`critique=`) so the worker wiring + persistence are
testable without a network call, and no commit here — the worker commits the row with the job's
DONE status + summed cost.

Known limitation (shared, not S4.3-specific): if a call raises *after* a billed response (e.g. the
critic returns an unparsable response mid-loop), the handler raises and the worker's retry-rollback
records none of the already-spent cost, so a retry re-spends. This is the same non-idempotent-cost
limitation documented for S1.1/S1.2; the proper fix is an incremental cost ledger on the worker, not
here. Bounded to the worker's MAX_ATTEMPTS retries, and the monthly budget pre-check caps cumulative
runaway on subsequent runs.

**Novelty (AC):** recent items already on the channel are fetched and fed into the generator prompt
so it avoids near-duplicates. Pre-S4.5 nothing is `published` yet, so "recent" is the most-recent
items in any non-terminal-failure state; this narrows to published content once S4.5 lands.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import Session, col, select

from app.ai.client import (
    CRITIC_MAX_TOKENS,
    CRITIC_MODEL,
    GEN_BLOG_MAX_TOKENS,
    GEN_MODEL,
    GEN_SOCIAL_MAX_TOKENS,
    BrandKit,
    CriticVerdict,
    build_client,
    critique_content,
    generate_blog_article,
    generate_social_post,
)
from app.ai.pricing import cost_cents
from app.config import settings
from app.models import ContentItem, Product, StrategyBrief
from app.models.content_item import _TERMINAL_FAILURE, ContentItemStatus
from app.modules.crank.crank import ContentType
from app.modules.crank.guard import check_content
from app.modules.strategy.brief import month_to_date_cost_cents
from app.worker import handler

RECENT_LIMIT = 5  # how many recent items to feed the generator for novelty
SPOT_CHECK_RATE = 0.10  # S4.9: random share of items flagged for async review (on top of the first)
_RECENT_BODY_CHARS = 500  # cap each recent item's text so the novelty block stays bounded
_SUPPORTED_CONTENT_TYPES = frozenset({ContentType.SOCIAL.value, ContentType.BLOG.value})
# Fixed system + user-instruction text the generators always send (see ai/client.py), on top of the
# variable brief/brand/novelty inputs — folded into the budget reservation so a nearly-exhausted
# budget can't pass the gate and then overspend. Deliberately generous → err toward refusing.
_FIXED_PROMPT_CHARS = 1200


@dataclass
class Generated:
    """Normalized generator output the handler persists as a ContentItem (decouples persistence
    from the per-type LLM schema, so a stub can drive the full worker path)."""

    body: str
    meta: dict  # serialized to content_item.meta_json (referenced pillar + per-type metadata)
    title: str | None = None  # blog headline; None for social


# generate(product, brief, brand_kit, content_type, recent_items) -> (Generated, cost_cents)
GenerateFn = Callable[[Product, StrategyBrief, BrandKit, str, list[str]], tuple[Generated, int]]
# critique(product, brand_kit, content_type, candidate) -> (CriticVerdict, cost_cents)
CritiqueFn = Callable[[Product, BrandKit, str, Generated], tuple[CriticVerdict, int]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _is_first_for_channel(session: Session, product_id: int, channel_id: int) -> bool:
    """True when no content item has yet been produced for this (product, channel) — its inaugural
    item, which S4.9 always flags for spot-check."""
    existing = session.exec(
        select(ContentItem.id)
        .where(ContentItem.product_id == product_id, ContentItem.channel_id == channel_id)
        .limit(1)
    ).first()
    return existing is None


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
        _require_known_pillar(post.pillar, pillars, "social")
        meta = {"pillar": post.pillar, "hashtags": post.hashtags}
        return Generated(body=post.body, meta=meta), cost

    if content_type == ContentType.BLOG.value:
        article, cost = generate_blog_article(
            build_client(), product.name, brand_kit, brief.positioning, pillars, recent_items
        )
        _require_known_pillar(article.pillar, pillars, "blog")
        meta = {
            "pillar": article.pillar,
            "slug": article.slug,
            "meta_description": article.meta_description,
        }
        return Generated(title=article.title, body=article.body, meta=meta), cost

    raise LookupError(f"no generator for content_type {content_type!r} (Phase A is social|blog)")


def _require_known_pillar(pillar: str, pillars: list[str], kind: str) -> None:
    """The AC requires generated metadata to reference a *real* content pillar. The model is told
    to pick from the brief's pillars, but nothing forces it — reject a hallucinated one so it's
    never persisted. RuntimeError (not LookupError) → the worker retries; the model usually
    complies on a second pass, and a persistently off-brand model fails the job rather than saving
    off-brand metadata."""
    if pillar not in pillars:
        raise RuntimeError(f"{kind} generation returned unknown pillar {pillar!r} (not in brief)")


def _real_critique(
    product: Product, brand_kit: BrandKit, content_type: str, candidate: Generated
) -> tuple[CriticVerdict, int]:
    return critique_content(
        build_client(), product.name, brand_kit, content_type, candidate.title, candidate.body
    )


def _reservation_input_estimate(
    product_name: str, brief: StrategyBrief, brand_json: str, recent_items: list[str]
) -> int:
    """Rough input-token estimate for the budget reservation. ~3 chars/token, deliberately low
    (→ higher token count → higher reserve → err toward refusing), matching brief.py. Counts every
    input the generators actually send: the variable brief/brand/novelty text, the product name,
    and the fixed system + instruction overhead (`_FIXED_PROMPT_CHARS`)."""
    chars = len(product_name) + len(brief.positioning) + len(brief.content_pillars_json)
    chars += len(brand_json) + sum(len(item) for item in recent_items) + _FIXED_PROMPT_CHARS
    return chars // 3


def _reserve_one_attempt(
    product_name: str,
    brief: StrategyBrief,
    brand_json: str,
    recent_items: list[str],
    content_type: str,
) -> int:
    """Worst-case cost of one generate + critic pass, for the budget gate. The critic reads the
    generated body (≈ the generator's output cap) plus the brand context, on the cheaper tier."""
    gen_max_out = (
        GEN_BLOG_MAX_TOKENS if content_type == ContentType.BLOG.value else GEN_SOCIAL_MAX_TOKENS
    )
    est_input = _reservation_input_estimate(product_name, brief, brand_json, recent_items)
    gen_reserve = cost_cents(GEN_MODEL, est_input, gen_max_out)
    critic_input = gen_max_out + len(brand_json) // 3 + 200  # body ≈ gen output + brand + overhead
    critic_reserve = cost_cents(CRITIC_MODEL, critic_input, CRITIC_MAX_TOKENS)
    return gen_reserve + critic_reserve


def run_generate(
    job,
    session: Session,
    *,
    generate: GenerateFn = _real_generate,
    critique: CritiqueFn = _real_critique,
    sample: Callable[[], float] = random.random,
) -> int:
    """Generate → critic+safety gate → persist one content item for the fanned-out cell (S4.2+S4.3).

    Loops up to `1 + critic_max_regenerations` times: generate a candidate, critique it, and either
    hard-block on a safety failure (`guard_failed`), accept on `score >= threshold`
    (`critic_passed`), or regenerate. Exhausting the attempts without passing skips+logs the last
    candidate (`critic_failed`). Exactly one ContentItem row is persisted per cell — the final
    candidate with its verdict. Returns the summed cost of all generate + critic calls (cents)."""
    if job.product_id is None or job.channel_id is None or job.content_type is None:
        raise LookupError(
            f"generate job {job.id} missing product_id/channel_id/content_type "
            "(should be set by the crank fan-out)"
        )
    # Validate the content type up front — before any budget math or client setup. An unknown type
    # is a wiring bug (Phase A is social|blog); routing it as "not blog → social" through the budget
    # gate would be wrong, and it must fail the same way with or without an API key.
    if job.content_type not in _SUPPORTED_CONTENT_TYPES:
        raise LookupError(
            f"generate job {job.id} has unsupported content_type {job.content_type!r} "
            "(Phase A is social|blog)"
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

    # Budget gate (mirrors brief.py): 0 = unset/unlimited. Pre-check blocks a run already over; then
    # reserve one full generate+critic pass before each attempt so a small remaining budget can't be
    # blown past — and a regeneration that no longer fits is dropped rather than overspending.
    budget = product.token_budget_cents_month
    remaining: int | None = None
    if budget > 0:
        spent = month_to_date_cost_cents(session, product.id, _utcnow())
        if spent >= budget:
            raise RuntimeError(
                f"product {product.id} over monthly token budget ({spent} >= {budget} cents)"
            )
        remaining = budget - spent
    reserve_per_attempt = _reserve_one_attempt(
        product.name, brief, product.brand_json, recent_items, job.content_type
    )

    total_cost = 0
    final_gen: Generated | None = None
    final_verdict: CriticVerdict | None = None
    status: ContentItemStatus | None = None
    guard_error: str | None = None  # S4.4 deterministic-guard failure reason, persisted on the row
    for _attempt in range(1 + settings.critic_max_regenerations):
        if remaining is not None and total_cost + reserve_per_attempt > remaining:
            if (
                final_gen is None
            ):  # can't even afford the first pass — fail loudly (no partial spend)
                raise RuntimeError(
                    f"insufficient budget to reserve a generate+critic pass for product "
                    f"{product.id} (need ~{reserve_per_attempt}, have {remaining} cents)"
                )
            break  # can't afford another regeneration → keep the last (low-scoring) candidate
        gen, gen_cost = generate(product, brief, brand_kit, job.content_type, recent_items)
        total_cost += gen_cost
        verdict, critic_cost = critique(product, brand_kit, job.content_type, gen)
        total_cost += critic_cost
        final_gen, final_verdict = gen, verdict
        if not verdict.safety_pass:  # AC: hard block, no regeneration
            status = ContentItemStatus.GUARD_FAILED
            break
        if verdict.score >= settings.critic_score_threshold:  # AC: passed the quality bar
            # S4.4: deterministic guard runs on the critic-approved candidate, independent of the
            # LLM. A failure is a hard block (like safety_pass=False) — no regeneration, log + skip.
            guard_error = check_content(gen.title, gen.body, brief, product)
            status = (
                ContentItemStatus.GUARD_FAILED
                if guard_error is not None
                else ContentItemStatus.CRITIC_PASSED
            )
            break
        # low score → the loop falls through and regenerates if an attempt (and budget) remains

    if status is None:  # exhausted attempts / budget-stopped without passing → skip+log
        status = ContentItemStatus.CRITIC_FAILED

    # S4.9: flag for async review — the channel's first item always, plus a random SPOT_CHECK_RATE
    # share. Set once here at creation; it never touches `status`, so it can't block publishing.
    spot_check = (
        _is_first_for_channel(session, product.id, job.channel_id) or sample() < SPOT_CHECK_RATE
    )

    # final_gen/final_verdict are always set here: the only path that runs no attempt raises above.
    assert final_gen is not None and final_verdict is not None
    session.add(
        ContentItem(
            product_id=product.id,
            channel_id=job.channel_id,
            content_type=job.content_type,
            status=status,
            title=final_gen.title,
            body=final_gen.body,
            meta_json=json.dumps(final_gen.meta),
            critic_score=final_verdict.score,
            critic_notes=final_verdict.notes,
            error=guard_error,
            spot_check=spot_check,
        )
    )
    # No commit here: the worker commits the content item atomically with the job's DONE status +
    # token_cost_cents (committing early would let a crash requeue the job and double-spend).
    return total_cost


# Indirection so tests can drive the full enqueue → run_due_jobs path with stub generate + critic
# (no network), while production uses the real LLM implementations.
_GENERATE: GenerateFn = _real_generate
_CRITIQUE: CritiqueFn = _real_critique


@handler("generate")
def _generate_handler(job, session: Session) -> int:
    return run_generate(job, session, generate=_GENERATE, critique=_CRITIQUE)
