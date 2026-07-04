"""channel — one publishing channel per product (TECH_SPEC §4, story S2.6).

1:N with product; one row per channel type per product (unique constraint). v1 owned-first:
blog/reddit are autonomous, x/instagram/youtube are enabled-but-human-assisted (Revision 0.2).
The engine-prepared profile (handles, bio, profile copy, warm-up note) is *folded* onto
`profile_json` — the v1 folded-JSON pattern (matches product.brand_json); it only becomes a table
if richer per-channel modeling materializes. No FK on product_id in v1 (matches job_run/credential).
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel, UniqueConstraint


class ChannelType(StrEnum):
    BLOG = "blog"
    REDDIT = "reddit"
    X = "x"
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"


class ConnectState(StrEnum):
    PENDING = "pending"  # no token yet
    CONNECTED = "connected"  # OAuth token in the vault
    FAILED = "failed"  # token dead/expired (set by S4.8 refresh)


# v1: only these post autonomously; the rest are enabled but human-assisted (Revision 0.2).
# S5.1 makes YOUTUBE autonomous — the short-form video pipeline uploads rendered MP4s end-to-end.
AUTONOMOUS_TYPES = frozenset({ChannelType.BLOG, ChannelType.REDDIT, ChannelType.YOUTUBE})

# Providers whose stored credential is a structured self-managed blob (the provider's own client
# refreshes access tokens under the hood) rather than a bare access token we hold and refresh.
# Reddit stores PRAW kwargs as JSON under `reddit_oauth`; `/connect` writes that shape and
# `oauth_refresh.is_self_managed_credential` classifies it so proactive refresh is skipped (S4.8.1).
SELF_MANAGED_TYPES = frozenset({ChannelType.REDDIT})


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Channel(SQLModel, table=True):
    __tablename__ = "channel"
    __table_args__ = (UniqueConstraint("product_id", "type", name="uq_channel_product_type"),)

    id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(index=True)
    type: ChannelType
    enabled: bool = True
    autonomous: bool = False
    account_ref: str | None = None  # handle/username once the human creates the account
    connect_state: ConnectState = Field(default=ConnectState.PENDING)
    daily_cap: int | None = None  # per-channel pacing cap (§7); None = unset
    paused: bool = False  # per-channel kill switch (S4.6)
    profile_json: str | None = None  # folded {handle, bio, profile_copy, warmup_note}, JSON-encoded
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
