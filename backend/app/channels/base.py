"""Channel adapter contract (TECH_SPEC §7, story S4.5).

Uniform, **API-first** publishing interface — no browser fallback in v1. Each adapter turns one
vetted `content_item` into a live post and can `delete` it again (retract, S4.7). `publish` MUST be
idempotent on `item.idempotency_key` (check the remote before re-posting); transient failures raise
`Retryable` so the publish pass leaves the item `scheduled` and re-attempts on the next tick.

The adapter is handed the item, its `product` (blog needs the slug/domain to place the file), the
`channel` (reddit reads its target subreddit/flair from `profile_json`), and the already-decrypted
credential blob it declared via `credential_key` (None for the owned blog).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.models import Channel, ContentItem, Product
from app.models.channel import ChannelType


class Retryable(Exception):
    """A transient publish failure (network, rate-limit). The publish pass keeps the item
    `scheduled` and retries next tick rather than marking it `publish_failed`."""


class AuthFailure(Exception):
    """A dead/revoked OAuth credential surfaced at publish time (e.g. a self-managed refresh token
    the provider's client refreshes internally). The publish pass fences the whole channel
    (`connect_state=failed` + alert) and leaves the item `scheduled` so it resumes on reconnect —
    the S4.8 fail-safe for providers whose token we don't refresh ourselves."""


@dataclass
class PublishResult:
    external_url: str


class ChannelAdapter(Protocol):
    type: ChannelType
    # Logical vault key for this channel's secret (see secrets.vault.get_credential); None when the
    # adapter needs no credential (owned blog writes to local disk).
    credential_key: str | None

    def publish(
        self, item: ContentItem, product: Product, channel: Channel, creds: str | None
    ) -> PublishResult: ...

    def delete(
        self, external_url: str, product: Product, channel: Channel, creds: str | None
    ) -> None: ...


def get_adapter(channel_type: ChannelType) -> ChannelAdapter:
    """Return the v1 adapter for an autonomous channel type. Unknown/deferred types raise."""
    # Imported here (not at module top) so importing the contract never drags in praw.
    from app.channels.blog import BlogAdapter
    from app.channels.reddit import RedditAdapter
    from app.channels.youtube import YouTubeAdapter

    adapters: dict[ChannelType, ChannelAdapter] = {
        ChannelType.BLOG: BlogAdapter(),
        ChannelType.REDDIT: RedditAdapter(),
        ChannelType.YOUTUBE: YouTubeAdapter(),
    }
    adapter = adapters.get(channel_type)
    if adapter is None:
        raise LookupError(f"no v1 publish adapter for channel type {channel_type!r}")
    return adapter
