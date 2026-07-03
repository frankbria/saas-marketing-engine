"""ORM models (TECH_SPEC §4). Import each so it registers on SQLModel.metadata."""

from app.models.channel import (
    AUTONOMOUS_TYPES,
    SELF_MANAGED_TYPES,
    Channel,
    ChannelType,
    ConnectState,
)
from app.models.content_item import ContentItem, ContentItemStatus
from app.models.credential import Credential
from app.models.funnel_event import FunnelEvent, FunnelEventType
from app.models.gpu_lease import GpuLease, GpuLeaseStatus
from app.models.heartbeat_digest import HeartbeatDigest
from app.models.job_run import JobRun, JobStatus
from app.models.metric_event import MetricEvent, MetricStage
from app.models.product import LifecycleState, MonetizationModel, Product
from app.models.qa_checklist_item import QaChecklistItem, QaItemStatus
from app.models.setup_checklist_item import SetupChecklistItem, SetupItemStatus
from app.models.strategy_brief import StrategyBrief

__all__ = [
    "AUTONOMOUS_TYPES",
    "SELF_MANAGED_TYPES",
    "Channel",
    "ChannelType",
    "ConnectState",
    "ContentItem",
    "ContentItemStatus",
    "Credential",
    "FunnelEvent",
    "FunnelEventType",
    "GpuLease",
    "GpuLeaseStatus",
    "HeartbeatDigest",
    "JobRun",
    "JobStatus",
    "LifecycleState",
    "MetricEvent",
    "MetricStage",
    "MonetizationModel",
    "Product",
    "QaChecklistItem",
    "QaItemStatus",
    "SetupChecklistItem",
    "SetupItemStatus",
    "StrategyBrief",
]
