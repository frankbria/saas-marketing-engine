"""heartbeat_digest — one daily observability snapshot per product (TECH_SPEC §8.4, story S6.2).

Per-channel counts (published / failed / reach) folded onto `digest_json` and the alerts fired
folded onto `alerts_json` — the v1 folded-JSON pattern (matches channel.profile_json). The row is
the operator's Flower replacement: the private API reads it back, and its existence for a given UTC
day is the idempotency guard that keeps a restarted scheduler from double-sending. No FK on
product_id in v1 (matches job_run/credential/channel).
"""

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class HeartbeatDigest(SQLModel, table=True):
    __tablename__ = "heartbeat_digest"

    id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(index=True)  # no FK in v1

    # 24h reporting window this digest covers; `window_end` is the run time and carries the
    # UTC-day idempotency check (indexed for the daily "already ran?" lookup).
    window_start: datetime
    window_end: datetime = Field(index=True)

    digest_json: str  # folded {"channels": [{channel_id, channel_type, published, failed, reach}]}
    alerts_json: str  # folded [{kind, message, channel_id, channel_type}]

    created_at: datetime = Field(default_factory=_utcnow)
