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
    # Whether this platform exposes an engagement counter we can poll back (S6.2.1). False for
    # owned infra (blog, podcast): there is no third party to hide our posts, so those channels are
    # never polled and are excluded from the zero-reach shadowban alert rather than silently
    # passing it. This is a *static* declaration — the heartbeat needs the answer without holding
    # an item or a credential — whereas `fetch_reach` answers per item, per tick.
    has_platform_reach: bool

    def publish(
        self, item: ContentItem, product: Product, channel: Channel, creds: str | None
    ) -> PublishResult: ...

    def delete(
        self, external_url: str, product: Product, channel: Channel, creds: str | None
    ) -> None: ...

    def fetch_reach(
        self, item: ContentItem, product: Product, channel: Channel, creds: str | None
    ) -> int | None:
        """This item's **cumulative** engagement count on the platform, or None if unavailable.

        Cumulative (not a delta) because that is what the platforms report; `poll_reach` diffs it
        against what it has already recorded.

        `None` means the platform has no number for this item — it was never published, the post
        has been deleted, or the response shape drifted. Failures are *not* folded into `None`:
        transient ones raise `Retryable` and dead credentials raise `AuthFailure`, exactly as in
        `publish`, so the poll can tell "the platform says nobody saw it" apart from "we could not
        ask". Only the first of those may ever reach the zero-reach alert.
        """
        ...


def has_platform_reach(channel_type: ChannelType) -> bool:
    """Whether this channel type has a platform engagement counter worth reading (S6.2.1/#79).

    False for owned infra (blog, podcast) and for types with no v1 adapter (x, instagram). For
    those, reach is **unmeasured** — which is not the same as zero, and must never be rendered or
    alerted on as if it were. Every reader of reach (the poll, the shadowban alert, the funnel
    rollup) routes through here so "unmeasured" means one thing across the app.
    """
    try:
        return get_adapter(channel_type).has_platform_reach
    except LookupError:
        return False


def get_adapter(channel_type: ChannelType) -> ChannelAdapter:
    """Return the v1 adapter for an autonomous channel type. Unknown/deferred types raise."""
    # Imported here (not at module top) so importing the contract never drags in praw.
    from app.channels.blog import BlogAdapter
    from app.channels.podcast import PodcastAdapter
    from app.channels.reddit import RedditAdapter
    from app.channels.youtube import YouTubeAdapter

    adapters: dict[ChannelType, ChannelAdapter] = {
        ChannelType.BLOG: BlogAdapter(),
        ChannelType.REDDIT: RedditAdapter(),
        ChannelType.YOUTUBE: YouTubeAdapter(),
        ChannelType.PODCAST: PodcastAdapter(),
    }
    adapter = adapters.get(channel_type)
    if adapter is None:
        raise LookupError(f"no v1 publish adapter for channel type {channel_type!r}")
    return adapter
