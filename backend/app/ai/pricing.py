"""Token → cents pricing for the models the engine calls (TECH_SPEC §5 cost tracking).

Rates are USD per 1M tokens from the published Claude pricing. Kept as a small table here
rather than a config knob — they change rarely and a wrong rate should fail review, not silently
read from env. ponytail: add a config override only if per-deployment rates ever diverge.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Rate:
    input_per_mtok: float  # USD per 1M input tokens
    output_per_mtok: float  # USD per 1M output tokens


# Only the tiers S1.1 uses; extend as the crank adds models.
RATES: dict[str, Rate] = {
    "claude-opus-4-8": Rate(input_per_mtok=5.0, output_per_mtok=25.0),
    "claude-haiku-4-5": Rate(input_per_mtok=1.0, output_per_mtok=5.0),
}


def cost_cents(model: str, input_tokens: int, output_tokens: int) -> int:
    """Cost of one call in whole cents, rounded up so we never under-bill the budget."""
    from math import ceil

    try:
        rate = RATES[model]
    except KeyError as exc:
        raise KeyError(f"no pricing for model {model!r}; add it to app.ai.pricing.RATES") from exc

    dollars = (
        input_tokens * rate.input_per_mtok + output_tokens * rate.output_per_mtok
    ) / 1_000_000
    return ceil(dollars * 100)
