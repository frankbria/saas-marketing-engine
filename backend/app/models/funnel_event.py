"""funnel_event — raw public-funnel ingest record (TECH_SPEC §6, S2.2).

One table backs both the `visit` and `lead` endpoints; `event_type` distinguishes
them and `email` is only set for leads. Like the other v1 tables, `product_id` carries
no FK — the seam stays clean for Phase B. S2.4 (welcome email) and S2.5 (attribution
join: first_touch_token → Stripe client_reference_id) read/extend these rows.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


class FunnelEventType(StrEnum):
    VISIT = "visit"
    LEAD = "lead"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FunnelEvent(SQLModel, table=True):
    __tablename__ = "funnel_event"

    id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(index=True)
    event_type: FunnelEventType = Field(index=True)

    email: str | None = None  # leads only

    # First-touch attribution token (S2.5) + UTM params captured by the landing site.
    first_touch_token: str | None = Field(default=None, index=True)
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    utm_term: str | None = None

    created_at: datetime = Field(default_factory=_utcnow)
