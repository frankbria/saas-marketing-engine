"""qa_checklist_item — the human click-through QA gate (TECH_SPEC §4, stories S3.1/S3.2).

S3.1 generates concrete, ordered "open X, click Y, verify Z" steps a non-technical tester runs
to verify the product + payment funnel; S3.2 lets the tester mark each pass/fail with a comment
and blocks go-live until every blocking item passes. Distinct from `setup_checklist_item` (a
done/pending setup punch-list) and the launch checklist (a deterministic readiness rollup folded
onto the product): this is a pass/fail product-QA gate with its own rows. No FK in v1.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


class QaItemStatus(StrEnum):
    PENDING = "pending"
    PASS = "pass"  # member name PASS is fine; only lowercase `pass` is a keyword
    FAIL = "fail"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class QaChecklistItem(SQLModel, table=True):
    __tablename__ = "qa_checklist_item"

    id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(index=True)
    ord: int  # 1-based display order
    instruction: str  # concrete "open X, click Y, verify Z" step
    blocking: bool = Field(default=True)  # a failing blocking item blocks go-live (S3.2)
    status: QaItemStatus = Field(default=QaItemStatus.PENDING)
    comment: str | None = Field(default=None)  # tester note on pass/fail (S3.2)
    updated_at: datetime = Field(default_factory=_utcnow)
