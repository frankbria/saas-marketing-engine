"""Anthropic calls for the strategy module (TECH_SPEC §5/§9).

Two tiers per §9: a cheap model for per-file summarization (bulk), Opus for the synthesis that
produces the brief. The brief shape is a Pydantic schema enforced via structured outputs, so the
model returns validated JSON rather than free text we'd have to parse.

ponytail: this is a single structured generation, not an agent loop — plain Messages API, no
Managed Agents. Each call returns (data, cost_cents) so the worker can sum cost for job_run.
"""

from __future__ import annotations

from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from app.ai.pricing import cost_cents
from app.config import settings

SUMMARY_MODEL = "claude-haiku-4-5"  # bulk per-file summaries
SYNTHESIS_MODEL = "claude-opus-4-8"  # strategy synthesis (§9)
SYNTHESIS_MAX_TOKENS = 8000  # output cap for synthesis (also the budget-reservation ceiling)

BRAND_MODEL = "claude-opus-4-8"  # brand kit synthesis (S1.2)
BRAND_MAX_TOKENS = 2000  # output cap for the brand kit (also the budget-reservation ceiling)

PRICING_MODEL = "claude-opus-4-8"  # pricing recommendation (S1.3)
PRICING_MAX_TOKENS = 1000  # output cap for pricing (also the budget-reservation ceiling)

SITE_MODEL = "claude-opus-4-8"  # landing-site copy + design tokens (S2.1)
SITE_MAX_TOKENS = 1500  # output cap for site content (also the budget-reservation ceiling)


class ICP(BaseModel):
    segment: str
    description: str
    firmographics: list[str]


class ChannelPlanItem(BaseModel):
    channel: str
    rationale: str
    priority: int


class Cadence(BaseModel):
    summary: str
    posts_per_week: int


class BriefDraft(BaseModel):
    """The Marketing Brief the synthesis call must return (TECH_SPEC §5)."""

    icp: ICP
    pain_points: list[str]
    positioning: str
    channel_plan: list[ChannelPlanItem]
    content_pillars: list[str]
    cadence: Cadence


class VoiceDescriptor(BaseModel):
    """One brand voice trait. `guidance` is concrete enough for S4.3 (critic) and S4.4 (guard)
    to apply it programmatically — the AC's "structured for later reuse"."""

    descriptor: str  # e.g. "confident", "playful"
    guidance: str  # how it shows up in copy (what to do / avoid)


class BrandKit(BaseModel):
    """On-brand kit folded onto product.brand_json (TECH_SPEC §5 step 3 / story S1.2)."""

    name: str  # brand / product name
    tone: str  # one-line overall tone
    voice_descriptors: list[VoiceDescriptor]
    visual_seeds: list[str]  # palette / imagery seed cues for the site template (§6)


class SiteContent(BaseModel):
    """Landing-site copy slots + concrete design tokens (S2.1, §6.1).

    The AI fills these from the product's Brand Kit; the site-template layout/plumbing stay
    constant. Colors are concrete hex (the kit's `visual_seeds` are textual cues, not values) so the
    template can drop them straight into CSS custom properties. `font_family` is a full CSS stack.
    """

    headline: str
    subhead: str
    value_props: list[str] = Field(min_length=1)  # benefit bullets
    cta_label: str  # primary call-to-action button label, e.g. "Start free"
    primary_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")  # brand primary, hex
    accent_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")  # brand accent, hex
    # CSS font stack rendered verbatim into a <style> block — HTML autoescaping does NOT make a
    # value safe in CSS context, so constrain it to font-name tokens (no ;{}()<> delimiters) to
    # block CSS injection from a malformed or prompt-injected model response.
    font_family: str = Field(pattern=r"^[\w ,.'\"-]+$", max_length=200)  # e.g. 'Georgia, serif'


class PricingRecommendation(BaseModel):
    """A cc_sub price recommendation folded onto product.price_* (TECH_SPEC §5 step 4 / S1.3).

    `price_interval` is constrained to the two intervals Stripe setup (S2.3) supports; the product
    column stays a plain string so future intervals don't need a schema change. Amount is in cents.
    """

    price_amount_cents: int = Field(gt=0)  # e.g. 2900 = $29.00
    price_interval: Literal["month", "year"]


def build_client() -> anthropic.Anthropic:
    key = settings.anthropic_api_key
    if key is None:
        raise RuntimeError("SME_ANTHROPIC_API_KEY is not set; cannot call the strategy LLM")
    return anthropic.Anthropic(api_key=key.get_secret_value())


def summarize_file(client: anthropic.Anthropic, relpath: str, content: str) -> tuple[str, int]:
    """One short summary of a single repo file. Returns (summary, cost_cents)."""
    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=512,
        system=(
            "You summarize one file from a software product's repo for marketing analysis. "
            "In 2-4 sentences, capture what the product does, who it's for, and any positioning "
            "or feature signal. Ignore boilerplate. Output prose only."
        ),
        messages=[{"role": "user", "content": f"File: {relpath}\n\n{content}"}],
    )
    summary = next((b.text for b in response.content if b.type == "text"), "")
    cost = cost_cents(SUMMARY_MODEL, response.usage.input_tokens, response.usage.output_tokens)
    return summary, cost


def synthesize_brief(
    client: anthropic.Anthropic,
    product_name: str,
    description: str | None,
    summaries: list[tuple[str, str]],
) -> tuple[BriefDraft, int]:
    """Synthesize the Marketing Brief from per-file summaries. Returns (brief, cost_cents)."""
    joined = "\n".join(f"- {path}: {summary}" for path, summary in summaries)
    user = (
        f"Product: {product_name}\n"
        f"Owner description: {description or '(none)'}\n\n"
        f"Per-file summaries of the product's codebase:\n{joined}\n\n"
        "Produce a Marketing Brief: ideal customer profile, pain points, positioning, a channel "
        "plan, at least 3 content pillars, and a posting cadence."
    )
    response = client.messages.parse(
        model=SYNTHESIS_MODEL,
        max_tokens=SYNTHESIS_MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=(
            "You are a senior B2C/SMB marketing strategist. From summaries of a product's "
            "codebase and the owner's description, derive a concrete, specific Marketing Brief. "
            "Be opinionated and grounded in the product's actual capabilities."
        ),
        messages=[{"role": "user", "content": user}],
        output_format=BriefDraft,
    )
    # The validated object rides on the text content block's `parsed_output` (adaptive thinking
    # may emit a thinking block first, so scan for the text block rather than indexing [0]).
    brief = next(
        (b.parsed_output for b in response.content if b.type == "text" and b.parsed_output),
        None,
    )
    if brief is None:  # refusal or unparsable — surface, don't persist an empty brief
        raise RuntimeError(
            f"strategy synthesis returned no brief (stop_reason={response.stop_reason})"
        )
    cost = cost_cents(SYNTHESIS_MODEL, response.usage.input_tokens, response.usage.output_tokens)
    return brief, cost


def generate_brand_kit(
    client: anthropic.Anthropic,
    product_name: str,
    description: str | None,
    positioning: str,
    content_pillars: list[str],
) -> tuple[BrandKit, int]:
    """Derive a brand kit from the product + its Marketing Brief. Returns (kit, cost_cents)."""
    pillars = ", ".join(content_pillars) or "(none)"
    user = (
        f"Product: {product_name}\n"
        f"Owner description: {description or '(none)'}\n"
        f"Positioning (from the Marketing Brief): {positioning}\n"
        f"Content pillars: {pillars}\n\n"
        "Produce a Brand Kit: the brand name, an overall tone, voice descriptors (each with "
        "concrete guidance for how it shows up in copy), and visual seeds (palette/imagery cues)."
    )
    response = client.messages.parse(
        model=BRAND_MODEL,
        max_tokens=BRAND_MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=(
            "You are a brand strategist. From a product and its marketing positioning, derive a "
            "concrete, on-brand kit. Voice descriptors must be specific and actionable so a "
            "downstream copy critic and a claim-trace guard can apply them mechanically."
        ),
        messages=[{"role": "user", "content": user}],
        output_format=BrandKit,
    )
    # Same as synthesize_brief: adaptive thinking may emit a thinking block first, so scan for the
    # text block carrying the validated object rather than indexing [0].
    kit = next(
        (b.parsed_output for b in response.content if b.type == "text" and b.parsed_output),
        None,
    )
    if kit is None:  # refusal or unparsable — surface, don't persist an empty kit
        raise RuntimeError(
            f"brand kit generation returned nothing (stop_reason={response.stop_reason})"
        )
    cost = cost_cents(BRAND_MODEL, response.usage.input_tokens, response.usage.output_tokens)
    return kit, cost


def generate_site_content(
    client: anthropic.Anthropic,
    product_name: str,
    description: str | None,
    brand_kit: BrandKit,
    positioning: str,
) -> tuple[SiteContent, int]:
    """Write landing-site copy + design tokens from the Brand Kit. Returns (content, cost_cents)."""
    voice = (
        "; ".join(f"{d.descriptor}: {d.guidance}" for d in brand_kit.voice_descriptors) or "(none)"
    )
    seeds = ", ".join(brand_kit.visual_seeds) or "(none)"
    user = (
        f"Product: {product_name}\n"
        f"Owner description: {description or '(none)'}\n"
        f"Positioning: {positioning or '(none)'}\n"
        f"Brand tone: {brand_kit.tone}\n"
        f"Brand voice: {voice}\n"
        f"Visual seeds (palette/imagery cues): {seeds}\n\n"
        "Write the landing-page copy (headline, subhead, a few value-prop bullets, a CTA label) "
        "on-brand for this voice, and pick concrete design tokens — a primary and accent color as "
        "#RRGGBB hex grounded in the visual seeds, and a CSS font stack that fits the brand."
    )
    response = client.messages.parse(
        model=SITE_MODEL,
        max_tokens=SITE_MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=(
            "You are a conversion copywriter and brand designer. From a product and its brand kit, "
            "produce concise, on-brand landing copy and concrete visual tokens. Colors must be "
            "valid #RRGGBB hex; the font stack must be web-safe (no external font hosting)."
        ),
        messages=[{"role": "user", "content": user}],
        output_format=SiteContent,
    )
    # adaptive thinking may emit a thinking block first — scan for the text block with the object.
    content = next(
        (b.parsed_output for b in response.content if b.type == "text" and b.parsed_output),
        None,
    )
    if content is None:  # refusal or unparsable — surface, don't render an empty site
        raise RuntimeError(
            f"site content generation returned nothing (stop_reason={response.stop_reason})"
        )
    cost = cost_cents(SITE_MODEL, response.usage.input_tokens, response.usage.output_tokens)
    return content, cost


def recommend_pricing(
    client: anthropic.Anthropic,
    product_name: str,
    description: str | None,
    positioning: str,
    icp_segment: str,
) -> tuple[PricingRecommendation, int]:
    """Recommend a cc_sub price from the product + Marketing Brief. Returns (rec, cost_cents)."""
    user = (
        f"Product: {product_name}\n"
        f"Owner description: {description or '(none)'}\n"
        f"Positioning (from the Marketing Brief): {positioning}\n"
        f"Ideal customer profile: {icp_segment or '(none)'}\n\n"
        "Recommend a single credit-card-upfront subscription price for this product: the amount in "
        "US cents and a billing interval (monthly or yearly). Pick a price the target customer "
        "would plausibly pay for the value delivered."
    )
    response = client.messages.parse(
        model=PRICING_MODEL,
        max_tokens=PRICING_MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=(
            "You are a SaaS pricing strategist. From a product and its marketing positioning, "
            "recommend one concrete cc_sub price grounded in the value delivered and the ideal "
            "customer's willingness to pay. Return the amount in cents and a billing interval."
        ),
        messages=[{"role": "user", "content": user}],
        output_format=PricingRecommendation,
    )
    # Same as the other parse calls: adaptive thinking may emit a thinking block first, so scan for
    # the text block carrying the validated object rather than indexing [0].
    rec = next(
        (b.parsed_output for b in response.content if b.type == "text" and b.parsed_output),
        None,
    )
    if rec is None:  # refusal or unparsable — surface, don't persist an empty price
        raise RuntimeError(
            f"pricing recommendation returned nothing (stop_reason={response.stop_reason})"
        )
    cost = cost_cents(PRICING_MODEL, response.usage.input_tokens, response.usage.output_tokens)
    return rec, cost
