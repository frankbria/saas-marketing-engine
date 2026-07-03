"""gpu_lease — one row per ephemeral GPU pod rental (S5.0, issue #28).

Both the observability record ("what pods did we rent, when, why did teardown fail")
and the pod-minutes ledger the monthly media-compute cap sums over. Deliberately not a
`job_run`: those rows are per-product work items, while a lease is global infrastructure
shared by whatever media jobs are queued.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


class GpuLeaseStatus(StrEnum):
    ACTIVE = "active"
    ENDED = "ended"  # teardown verified at the provider — billing stopped
    TEARDOWN_UNVERIFIED = "teardown_unverified"  # DELETE accepted but pod still visible


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GpuLease(SQLModel, table=True):
    __tablename__ = "gpu_lease"

    id: int | None = Field(default=None, primary_key=True)
    provider: str  # e.g. "runpod"
    pod_id: str = Field(index=True)
    status: GpuLeaseStatus = Field(default=GpuLeaseStatus.ACTIVE, index=True)
    started_at: datetime = Field(default_factory=_utcnow)
    ended_at: datetime | None = None
    # Set on the first tick that observes the queue idle; cleared when work arrives.
    # Persisted (not in-memory) so a control-plane restart doesn't reset the idle clock.
    idle_since: datetime | None = None
    # Filled at close: lease minutes × the configured per-minute rate. Active leases
    # accrue dynamically in month_to_date_gpu_cost_cents instead.
    cost_cents: int = 0
