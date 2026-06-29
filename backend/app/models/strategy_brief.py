"""strategy_brief — the Marketing Brief, single source of truth for the crank (TECH_SPEC §4/§5).

1:1 with product (S1.1 produces it). The JSON-bearing fields hold engine-shaped structures
serialized as JSON strings — SQLite has no native JSON type and v1 keeps the data layer plain.
brand_json (S1.2) and pricing (S1.3) land on `product`, not here. `raw_ai_output` is kept for
debugging the generation that produced the row (§5 "Quality").
"""

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class StrategyBrief(SQLModel, table=True):
    __tablename__ = "strategy_brief"

    id: int | None = Field(default=None, primary_key=True)
    # 1:1 with product; no FK in v1 (matches job_run/credential).
    product_id: int = Field(index=True, unique=True)

    icp_json: str  # ideal customer profile (JSON-encoded)
    pain_points_json: str  # list of pain points (JSON-encoded)
    positioning: str
    channel_plan_json: str  # per-channel plan (JSON-encoded)
    content_pillars_json: str  # list of content pillars (JSON-encoded)
    cadence_json: str  # posting cadence (JSON-encoded)

    approved: bool = False  # owner approves in S1.4 → lifecycle setup_ready
    approved_at: datetime | None = None
    raw_ai_output: str | None = None  # full model output for debugging (§5)

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
