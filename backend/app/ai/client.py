"""Anthropic calls for the strategy module (TECH_SPEC §5/§9).

Two tiers per §9: a cheap model for per-file summarization (bulk), Opus for the synthesis that
produces the brief. The brief shape is a Pydantic schema enforced via structured outputs, so the
model returns validated JSON rather than free text we'd have to parse.

ponytail: this is a single structured generation, not an agent loop — plain Messages API, no
Managed Agents. Each call returns (data, cost_cents) so the worker can sum cost for job_run.
"""

from __future__ import annotations

import anthropic
from pydantic import BaseModel

from app.ai.pricing import cost_cents
from app.config import settings

SUMMARY_MODEL = "claude-haiku-4-5"  # bulk per-file summaries
SYNTHESIS_MODEL = "claude-opus-4-8"  # strategy synthesis (§9)
SYNTHESIS_MAX_TOKENS = 8000  # output cap for synthesis (also the budget-reservation ceiling)

BRAND_MODEL = "claude-opus-4-8"  # brand kit synthesis (S1.2)
BRAND_MAX_TOKENS = 2000  # output cap for the brand kit (also the budget-reservation ceiling)


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
