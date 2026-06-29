"""metric_event — funnel + attribution metric record (TECH_SPEC §4, S2.5).

One row per measured funnel event. The S2.5 attribution chain writes `stage=paid` rows joined back
from a Stripe `checkout.session.completed` webhook (UTM → lead → Stripe `client_reference_id` →
here). `channel_id`/`content_item_id` are nullable seams: those tables don't exist until P4 (S4.x),
so the honest attribution available in v1 is token → lead → product. Like the other v1 tables,
`product_id` carries no FK — kept clean for the Phase B Postgres swap.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


class MetricStage(StrEnum):
    IMPRESSION = "impression"
    VISIT = "visit"
    SIGNUP = "signup"
    PAID = "paid"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MetricEvent(SQLModel, table=True):
    __tablename__ = "metric_event"

    id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(index=True)

    # Channel/content attribution lands when those tables exist (S4.x); null until then.
    channel_id: int | None = Field(default=None, index=True)
    content_item_id: int | None = Field(default=None, index=True)

    stage: MetricStage = Field(index=True)
    value: int = 0  # cents for `paid`; a count for the other stages
    occurred_at: datetime = Field(default_factory=_utcnow)

    # Provenance + idempotency key, e.g. "stripe:cs_test_123" (Stripe redelivers webhook events).
    # `unique` makes the dedup race-proof at the DB boundary: a concurrent redelivery that slips
    # past the app-level pre-check hits a constraint violation instead of double-counting revenue.
    # NULLs stay distinct (SQLite + Postgres), so non-stripe stages are free to omit a source.
    source: str | None = Field(default=None, index=True, unique=True)
