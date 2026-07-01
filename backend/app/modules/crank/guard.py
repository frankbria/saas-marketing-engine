"""Deterministic guard — non-LLM safety net after the critic (story S4.4 / TECH_SPEC §8.2 / FR-23).

A hallucinated-but-on-brand item can sail past the same-family critic; this pass is independent of
any LLM. Two checks, both hard-block on failure (never publish, just log):

1. **Blocklist/regex** — configurable red-flag patterns (`settings.guard_blocklist`), e.g. absolute
   guarantees / compliance-risky claims.
2. **Claim-trace** — every *factual claim* in the item must trace to the strategy brief / product
   facts. Full claim extraction needs NLP; the deterministic proxy here is **numeric** claims — the
   high-signal, cheaply-extractable ones a fabricator invents (`50% faster`, `10x`, `$99`, `trusted
   by 10,000 teams`). A claim number absent from the fact corpus is untraceable ⇒ block.

ponytail: numeric-only claim-trace, known ceiling. It does not catch non-numeric fabrications
("the #1 tool") and treats any 4+ digit number (incl. a bare year) as a claim — a paranoid safety
net that errs toward blocking + logging for the async spot-check (S4.9), never toward publishing an
unverifiable claim. Upgrade path = NLP claim extraction / retrieval match if false-blocks bite.
"""

from __future__ import annotations

import re

from app.config import settings
from app.models import Product, StrategyBrief

# Numeric *claim* patterns — each captures the numeric core in group 1. Deliberately narrow so
# ordinary small counts ("5 tips", "Top 10") are not treated as claims (they carry no marker).
_PERCENT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*%")
_MULTIPLIER = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*[x×]\b", re.IGNORECASE)
_MONEY = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)")
_LARGE_COUNT = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d{4,})(?:\.\d+)?\b")  # 10,000 or 10000+

# Any run of digits (with thousands separators / decimals) — used to harvest the fact corpus.
_NUMBER_RUN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _norm_number(raw: str) -> str:
    """Canonicalize a numeric token so a claim matches its source: drop thousands separators and
    trailing-zero decimals ('10,000'→'10000', '99.00'→'99', '99.50'→'99.5')."""
    n = raw.replace(",", "")
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n


# Each claim pattern traces only to facts of the *same kind* — a `$99` price must not vouch for a
# `99%` or `99x` claim. `_LARGE_COUNT` is the catch-all bucket, tracing to any number in the facts.
_CLAIM_KINDS = (
    (_PERCENT, "percent"),
    (_MULTIPLIER, "multiplier"),
    (_MONEY, "money"),
    (_LARGE_COUNT, "count"),
)


def _typed_fact_numbers(brief: StrategyBrief, product: Product) -> dict[str, set[str]]:
    """Numbers the brief/product facts vouch for, bucketed by claim kind so a claim only traces to a
    same-kind fact. Percent/multiplier/money buckets come from same-marked numbers in the fact text;
    `count` is every number (the generic bucket). Price is money-only (stored cents + dollars)."""
    text = " ".join(
        [
            product.name,
            product.description or "",
            product.brand_json or "",
            brief.positioning,
            brief.content_pillars_json,
            brief.pain_points_json,
            brief.icp_json,
            brief.channel_plan_json,
            brief.cadence_json,
        ]
    )
    money = {_norm_number(m.group(1)) for m in _MONEY.finditer(text)}
    if product.price_amount_cents is not None:
        cents = product.price_amount_cents
        # Cover the cent count, whole dollars, and the dollars.cents form ($19.99 → "19.99") so
        # standard decimal pricing traces, not just whole-dollar prices.
        money |= {
            _norm_number(str(cents)),
            _norm_number(str(cents // 100)),
            _norm_number(f"{cents / 100:.2f}"),
        }
    return {
        "percent": {_norm_number(m.group(1)) for m in _PERCENT.finditer(text)},
        "multiplier": {_norm_number(m.group(1)) for m in _MULTIPLIER.finditer(text)},
        "money": money,
        "count": {_norm_number(m.group(0)) for m in _NUMBER_RUN.finditer(text)},
    }


def _blocklist_reason(text: str) -> str | None:
    for pattern in settings.guard_blocklist:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return f"blocked: matched blocklist term {m.group(0)!r}"
    return None


def _claim_trace_reason(text: str, brief: StrategyBrief, product: Product) -> str | None:
    facts = _typed_fact_numbers(brief, product)
    for pattern, kind in _CLAIM_KINDS:
        for m in pattern.finditer(text):
            claim = _norm_number(m.group(1))
            if claim not in facts[kind]:
                return f"blocked: untraceable claim {m.group(0)!r} (not in brief/product facts)"
    return None


def check_content(
    title: str | None, body: str, brief: StrategyBrief, product: Product
) -> str | None:
    """Run the deterministic guard on one item. Returns a human-readable failure reason (persisted
    to `content_item.error`) or None if it's clean. Pure: no LLM, no I/O."""
    text = f"{title or ''}\n{body}"
    return _blocklist_reason(text) or _claim_trace_reason(text, brief, product)
