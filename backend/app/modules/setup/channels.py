"""Channel accounts + human setup checklist handler (TECH_SPEC §6.5 / story S2.6).

Mirrors the S1.2/S2.1 handlers: budget pre-check → one structured Opus call for per-channel
handles/bios/profile copy (grounded in `product.brand_json`) → upsert `channel` rows with the
profile folded onto `channel.profile_json` (+ a deterministic warm-up note) → emit the ordered
human setup checklist (account/CAPTCHA, OAuth consent, ToS, DNS, SPF/DKIM/DMARC, Stripe/banking)
into `setup_checklist_item`. The checklist emission is fully deterministic (no tokens).

Which channels exist comes from the approved Marketing Brief's `channel_plan_json` (free-text names
mapped to the `ChannelType` enum). Re-running is idempotent: channels upsert by (product_id, type)
so a re-run never wipes a `connect_state`/credential, and checklist items upsert by
(product_id, channel_id, category) so a re-run never resets a `done` toggle.

The LLM work is injected (`generate`) so the worker wiring + persistence are testable without a
network call; the registered handler passes the real implementation.

ponytail: channels dropped from the plan on a re-run leave their (now-stale) rows behind — harmless
for a single owner who can ignore them; prune only if plan-churn ever proves noisy.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.ai.client import (
    CHANNEL_MAX_TOKENS,
    CHANNEL_MODEL,
    BrandKit,
    ChannelProfiles,
    build_client,
    generate_channel_profiles,
)
from app.ai.pricing import cost_cents
from app.models import (
    AUTONOMOUS_TYPES,
    Channel,
    ChannelType,
    Product,
    SetupChecklistItem,
    StrategyBrief,
)
from app.modules.strategy.brief import month_to_date_cost_cents
from app.worker import handler

# generate(product, brand_kit, channel_types, remaining_cents) -> (profiles, cost_cents)
GenerateFn = Callable[[Product, BrandKit, "list[str]", "int | None"], "tuple[ChannelProfiles, int]"]

# free-text plan name (lowercased) -> ChannelType. Checked most-specific first.
_TYPE_KEYWORDS: tuple[tuple[str, ChannelType], ...] = (
    ("reddit", ChannelType.REDDIT),
    ("youtube", ChannelType.YOUTUBE),
    ("instagram", ChannelType.INSTAGRAM),
    ("twitter", ChannelType.X),
    ("blog", ChannelType.BLOG),
    ("seo", ChannelType.BLOG),
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def map_channel_type(name: str) -> ChannelType | None:
    """Map a free-text channel-plan name to a ChannelType, or None if unrecognized."""
    n = name.lower()
    for keyword, ctype in _TYPE_KEYWORDS:
        if keyword in n:
            return ctype
    # "X" / "X (Twitter)" — match the bare token so it doesn't catch every word with an x in it.
    if "x" in {t for t in n.replace("/", " ").replace("(", " ").replace(")", " ").split()}:
        return ChannelType.X
    return None


def channel_types_from_brief(brief: StrategyBrief) -> list[ChannelType]:
    """Distinct ChannelTypes named in the brief's channel plan, ordered by plan priority."""
    items = json.loads(brief.channel_plan_json)
    # Lower `priority` first (matches ChannelPlanItem semantics); stable for items lacking it.
    ordered = sorted(items, key=lambda it: it.get("priority", 1_000_000))
    seen: dict[ChannelType, None] = {}
    for item in ordered:
        ctype = map_channel_type(item.get("channel", ""))
        if ctype is not None and ctype not in seen:
            seen[ctype] = None
    return list(seen)


def _warmup_note(ctype: ChannelType) -> str:
    return (
        f"New {ctype.value} account: post value-first, non-promotional content for ~1–2 weeks "
        "before sharing any product links (cold-account ban mitigation)."
    )


def _real_generate(
    product: Product, brand_kit: BrandKit, channel_types: list[str], remaining_cents: int | None
) -> tuple[ChannelProfiles, int]:
    client = build_client()

    # Reserve a conservative upper bound before the Opus call so a small remaining budget can't be
    # blown past it (mirrors brand.py/site.py). ~3 chars/token under-estimates → higher reserve.
    if remaining_cents is not None:
        PROMPT_OVERHEAD_CHARS = 700
        input_chars = (
            len(product.name)
            + len(product.description or "")
            + len(product.brand_json or "")
            + sum(len(t) for t in channel_types)
            + PROMPT_OVERHEAD_CHARS
        )
        reserve = cost_cents(CHANNEL_MODEL, input_chars // 3, CHANNEL_MAX_TOKENS)
        if reserve > remaining_cents:
            raise RuntimeError(
                f"insufficient budget to reserve for channel profiles for product {product.id} "
                f"(need ~{reserve}, have {remaining_cents} cents)"
            )

    return generate_channel_profiles(
        client, product.name, product.description, brand_kit, channel_types
    )


def _upsert_channel(
    session: Session, product_id: int, ctype: ChannelType, profile: dict
) -> Channel:
    """Create or update the (product, type) channel, preserving connect_state/account_ref."""
    existing = session.exec(
        select(Channel).where(Channel.product_id == product_id, Channel.type == ctype)
    ).first()
    chan = existing or Channel(product_id=product_id, type=ctype)
    chan.enabled = True
    chan.autonomous = ctype in AUTONOMOUS_TYPES
    chan.profile_json = json.dumps(profile)
    chan.updated_at = _utcnow()
    session.add(chan)
    session.flush()  # assign chan.id without committing (worker owns the commit)
    return chan


def _ensure_checklist_item(
    session: Session,
    product_id: int,
    *,
    channel_id: int | None,
    ord: int,
    instruction: str,
    category: str,
) -> None:
    """Upsert a checklist item by (product_id, channel_id, category) — preserves `status`."""
    existing = session.exec(
        select(SetupChecklistItem).where(
            SetupChecklistItem.product_id == product_id,
            SetupChecklistItem.channel_id == channel_id,
            SetupChecklistItem.category == category,
        )
    ).first()
    item = existing or SetupChecklistItem(
        product_id=product_id, channel_id=channel_id, category=category, ord=ord, instruction=""
    )
    item.ord = ord
    item.instruction = instruction
    item.updated_at = _utcnow()
    session.add(item)


def setup_product_channels(job, session: Session, *, generate: GenerateFn = _real_generate) -> int:
    """Prepare channel rows + profiles + the human setup checklist. Returns token cost in cents."""
    if job.product_id is None:
        raise LookupError("setup_channels job has no product_id")
    product = session.get(Product, job.product_id)
    if product is None:
        raise LookupError(f"product {job.product_id} not found")

    # The brand kit grounds the profiles (S2.6 depends on S1.2); the brief names the channels.
    if not product.brand_json:
        raise RuntimeError(f"product {product.id} has no brand kit; run the brand kit first")
    brand_kit = BrandKit.model_validate_json(product.brand_json)

    brief = session.exec(
        select(StrategyBrief).where(StrategyBrief.product_id == product.id)
    ).first()
    if brief is None:
        raise RuntimeError(f"product {product.id} has no strategy brief; run the brief first")

    channel_types = channel_types_from_brief(brief)
    if not channel_types:
        raise RuntimeError(
            f"product {product.id} brief names no recognized channels; nothing to set up"
        )

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

    profiles, cost = generate(product, brand_kit, [c.value for c in channel_types], remaining)
    by_type = {p.type: p for p in profiles.profiles}

    # The schema only enforces "≥1 profile", so a response that omits/duplicates a channel would
    # otherwise persist a channel with empty handle/bio — violating the per-channel profile AC.
    # Surface it (job fails/retries) instead of saving blanks, like the brand-kit handler.
    missing = [c.value for c in channel_types if c.value not in by_type]
    if missing:
        raise RuntimeError(
            f"channel profile generation omitted profiles for {missing} (product {product.id})"
        )

    # Upsert a channel per recognized type, folding the profile + a deterministic warm-up note.
    channel_ids: dict[ChannelType, int] = {}
    for ctype in channel_types:
        prof = by_type[ctype.value]
        folded = {
            "handle": prof.handle,
            "bio": prof.bio,
            "profile_copy": prof.profile_copy,
            "warmup_note": _warmup_note(ctype),
        }
        chan = _upsert_channel(session, product.id, ctype, folded)
        channel_ids[ctype] = chan.id

    # Emit the ordered human checklist: per-channel account/OAuth/ToS, then product-wide DNS/email.
    ord = 0
    for ctype in channel_types:
        cid = channel_ids[ctype]
        handle = by_type[ctype.value].handle
        _ensure_checklist_item(
            session,
            product.id,
            channel_id=cid,
            ord=ord,
            category="account",
            instruction=(
                f"Create the {ctype.value} account (suggested handle: {handle or 'n/a'}) — solve "
                "any CAPTCHA. New accounts need a warm-up period before posting links."
            ),
        )
        ord += 1
        _ensure_checklist_item(
            session,
            product.id,
            channel_id=cid,
            ord=ord,
            category="tos",
            instruction=f"Review and accept the {ctype.value} Terms of Service / automation rules.",
        )
        ord += 1
        _ensure_checklist_item(
            session,
            product.id,
            channel_id=cid,
            ord=ord,
            category="oauth",
            instruction=(
                f"Connect {ctype.value} via OAuth in the dashboard (grant posting scope); the "
                "token is stored in the vault."
            ),
        )
        ord += 1

    domain = product.marketing_domain or "the marketing domain"
    for category, instruction in (
        ("dns", f"Point DNS for {domain} (A/CNAME) at the marketing site."),
        (
            "email_auth",
            f"Add SPF, DKIM, and DMARC records for {domain} so welcome mail isn't spam-binned.",
        ),
        (
            "payments",
            "Complete Stripe/banking onboarding (business details, bank account, payout schedule).",
        ),
    ):
        _ensure_checklist_item(
            session,
            product.id,
            channel_id=None,
            ord=ord,
            category=category,
            instruction=instruction,
        )
        ord += 1

    # No commit here: the worker commits the rows + job status + token_cost_cents atomically (a
    # handler commit would let a crash before that double-spend on retry; matches brand.py/site.py).
    return cost


# Indirection so tests can drive the full enqueue → run_due_jobs path with a stub generator
# (no network), while production uses the real LLM implementation.
_GENERATE: GenerateFn = _real_generate


@handler("setup_channels")
def _setup_channels_handler(job, session: Session) -> int:
    return setup_product_channels(job, session, generate=_GENERATE)
