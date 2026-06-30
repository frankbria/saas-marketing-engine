"""product — the first-class unit of the engine (TECH_SPEC §4).

Every product-specific value (repo, domain, brand, pricing, budget) lives on this
record per PRD G7 (zero product-specific hardcoding). brand_json and the pricing
fields are intentionally *folded* onto the row in v1 — they only become separate
tables if multi-plan pricing or richer brand modeling materializes (TECH_SPEC §4).
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


class MonetizationModel(StrEnum):
    CC_SUB = "cc_sub"  # v1 implements this one; the others are enum-only until later phases
    TRIAL = "trial"
    FREEMIUM = "freemium"


class LifecycleState(StrEnum):
    DRAFT = "draft"
    STRATEGY = "strategy"
    SETUP_READY = "setup_ready"
    SETUP_DONE = "setup_done"
    QA = "qa"
    LIVE = "live"
    PAUSED = "paused"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Product(SQLModel, table=True):
    __tablename__ = "product"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    slug: str = Field(index=True, unique=True)
    repo_url: str | None = None
    repo_local_path: str | None = None
    description: str | None = None

    monetization_model: MonetizationModel = Field(default=MonetizationModel.CC_SUB)

    brand_json: str | None = None  # folded brand kit (S1.2); JSON-encoded
    price_amount_cents: int | None = None  # folded pricing (S1.3 / cc_sub)
    price_interval: str | None = None
    stripe_price_id: str | None = None

    marketing_domain: str | None = None
    token_budget_cents_month: int = Field(default=0, ge=0)  # per-product hard cap (§5)

    # Crank cadence (S4.1): how often the scheduler enqueues a crank for this product.
    # None ⇒ the weekly default (TECH_SPEC §8.1). Richer per-post cadence lives on strategy_brief.
    crank_cadence_seconds: int | None = None

    lifecycle_state: LifecycleState = Field(default=LifecycleState.DRAFT, index=True)
    # Latest pre-QA smoke-test result (S2.7), JSON-encoded `SmokeTestResult`; folded onto the row so
    # the dashboard reads it from the existing product GET (no separate table in v1).
    smoke_test_json: str | None = None
    # Launch checklist emitted from real setup output (S2.8), JSON-encoded `LaunchChecklist`;
    # emitting it crosses setup_done → qa. Folded like smoke_test_json (no table; qa_checklist_item
    # is S3.x).
    launch_checklist_json: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
