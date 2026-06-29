"""setup_checklist_item — irreducible human setup steps (TECH_SPEC §6.5, story S2.6).

The engine preps what it can per channel and emits only the human-required steps (CAPTCHA account
creation, OAuth consent, ToS, DNS + email auth, Stripe/banking) as an ordered checklist the owner
works through in the dashboard. Kept separate from `qa_checklist_item` (§3.x) on purpose: that is a
pass/fail product-QA gate; this is a done/pending setup punch-list with a different lifecycle.
`channel_id` is null for product-wide steps (DNS, SPF/DKIM/DMARC, Stripe/banking). No FK in v1.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


class SetupItemStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SetupChecklistItem(SQLModel, table=True):
    __tablename__ = "setup_checklist_item"

    id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(index=True)
    channel_id: int | None = Field(default=None, index=True)  # null = product-wide step
    ord: int  # display order
    instruction: str
    category: str  # account | oauth | tos | dns | email_auth | payments
    status: SetupItemStatus = Field(default=SetupItemStatus.PENDING)
    updated_at: datetime = Field(default_factory=_utcnow)
