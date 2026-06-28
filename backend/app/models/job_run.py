"""job_run — audit row for every scheduled/worker job (TECH_SPEC §4).

The in-process worker loop executes queued rows and tracks retries in `attempts`.
`product_id` is nullable and intentionally carries no FK in v1 — the `product` table
lands in S0.3; the seam stays clean so Phase B can add the constraint without a rewrite.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class JobRun(SQLModel, table=True):
    __tablename__ = "job_run"

    id: int | None = Field(default=None, primary_key=True)
    product_id: int | None = Field(default=None, index=True)  # no FK in v1 (see module docstring)
    kind: str = Field(index=True)
    status: JobStatus = Field(default=JobStatus.QUEUED, index=True)
    attempts: int = 0
    token_cost_cents: int = 0
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
