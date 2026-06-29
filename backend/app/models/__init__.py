"""ORM models (TECH_SPEC §4). Import each so it registers on SQLModel.metadata."""

from app.models.credential import Credential
from app.models.funnel_event import FunnelEvent, FunnelEventType
from app.models.job_run import JobRun, JobStatus
from app.models.product import LifecycleState, MonetizationModel, Product
from app.models.strategy_brief import StrategyBrief

__all__ = [
    "Credential",
    "FunnelEvent",
    "FunnelEventType",
    "JobRun",
    "JobStatus",
    "LifecycleState",
    "MonetizationModel",
    "Product",
    "StrategyBrief",
]
