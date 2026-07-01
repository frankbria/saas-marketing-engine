"""Pace + publish passes (TECH_SPEC §7, §8.2/§8.3, story S4.5).

Two deterministic periodic passes that carry a vetted item the last two steps of the pipeline —
`critic_passed → scheduled → published` — mirroring `enqueue_due_cranks` (pure, `now` injected, one
per-item state transition, trivially testable without the scheduler thread).

- `pace_content`: assigns each `critic_passed` item a spread `scheduled_for` + `idempotency_key`
  and flips it to `scheduled`. Pacing keeps ≤ `daily_cap` items landing per 24 h and spreads the
  rest across the product's cadence window, so no channel bursts (§7).
- `publish_scheduled`: publishes every `scheduled` item now due via its §7 channel adapter,
  re-checks the per-channel kill switch immediately before publishing, records the result + one
  metric row, and retries transient failures on the next tick. Each item commits independently, so
  one failure never blocks its siblings (§8.3 crash isolation).

Publish runs inline here rather than as a per-item `job_run`: an inline pass with per-item
`try/except` + per-item commit gives the same at-least-once + idempotency + isolation guarantees the
spec's "job_run worker loop" retry provides, without a new job kind or column. Repeated transient
failures escalate via the S6.2 heartbeat alert (§8.4), which is the intended operator signal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, select

from app.channels.base import AuthFailure, Retryable, get_adapter
from app.models import Channel, ContentItem, MetricEvent, MetricStage, Product
from app.models.channel import ConnectState
from app.models.content_item import ContentItemStatus
from app.modules.alerts import raise_alert
from app.modules.crank.crank import _cadence_seconds  # reuse the crank's cadence-window clamp
from app.modules.crank.oauth_refresh import (
    RefreshUnavailable,
    is_self_managed_credential,
    needs_refresh,
    refresh_channel_token,
)
from app.modules.metrics.utm import thread_utm_links
from app.secrets.vault import get_credential, get_credential_expiry


def _spacing_seconds(window_seconds: int, daily_cap: int | None, batch_size: int) -> float:
    """Seconds between consecutive scheduled items for one channel.

    With a `daily_cap`, step by `window / daily_cap` — for a weekly window and `daily_cap=7` that
    is one per day, so at most `daily_cap` items land in any 24 h while filling the window. With no
    cap, spread this batch evenly across the window. Never 0 (that would stack items at one time).
    """
    if daily_cap and daily_cap > 0:
        spacing = window_seconds / daily_cap
    else:
        spacing = window_seconds / max(batch_size, 1)
    return max(spacing, 1.0)


def _last_scheduled_at(session: Session, channel_id: int) -> datetime | None:
    """The latest `scheduled_for` already assigned on this channel, so a new batch keeps spacing
    past the previous one instead of piling up at `now`."""
    row = session.exec(
        select(ContentItem)
        .where(
            ContentItem.channel_id == channel_id,
            col(ContentItem.scheduled_for).is_not(None),
            col(ContentItem.status).in_([ContentItemStatus.SCHEDULED, ContentItemStatus.PUBLISHED]),
        )
        .order_by(col(ContentItem.scheduled_for).desc())
    ).first()
    if row is None or row.scheduled_for is None:
        return None
    # SQLite hands datetimes back tz-naive; normalize to aware UTC so arithmetic against the aware
    # `now` (datetime.now(UTC)) doesn't raise offset-naive/offset-aware TypeErrors.
    at = row.scheduled_for
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def pace_content(session: Session, now: datetime) -> list[ContentItem]:
    """Schedule every `critic_passed` item on an active channel. Returns the newly-scheduled."""
    channels = session.exec(
        select(Channel).where(
            Channel.enabled,
            Channel.autonomous,
            ~Channel.paused,
            Channel.connect_state != ConnectState.FAILED,  # dead-token channels don't accrue work
        )
    ).all()

    scheduled: list[ContentItem] = []
    for channel in channels:
        items = session.exec(
            select(ContentItem)
            .where(
                ContentItem.channel_id == channel.id,
                ContentItem.status == ContentItemStatus.CRITIC_PASSED,
            )
            .order_by(col(ContentItem.created_at), col(ContentItem.id))
        ).all()
        product = session.get(Product, channel.product_id)
        if not items or product is None:
            continue

        window = _cadence_seconds(product)
        spacing = timedelta(seconds=_spacing_seconds(window, channel.daily_cap, len(items)))

        last = _last_scheduled_at(session, channel.id)
        cursor = now if last is None else max(now, last + spacing)
        for item in items:
            item.scheduled_for = cursor
            item.idempotency_key = f"{channel.type.value}:{item.id}"
            item.status = ContentItemStatus.SCHEDULED
            session.add(item)
            scheduled.append(item)
            cursor = cursor + spacing

    session.commit()
    return scheduled


def _fence_channel(session, channel, product, now, error: str) -> None:
    """S4.8 fail-safe: mark a channel `failed` (dead token) and fire an operator alert. Callers
    leave the current item `scheduled` so it resumes once the channel is reconnected. Assumes the
    caller has rolled back any partial work for the current item."""
    channel.connect_state = ConnectState.FAILED
    channel.updated_at = now
    session.add(channel)
    session.commit()
    raise_alert(
        "oauth_refresh_failed",
        f"{channel.type.value} token failed; halting publishes until reconnected",
        product_id=product.id,
        channel_id=channel.id,
        error=error,
    )


def _refresh_if_needed(session, product, channel, credential_key, now, refresh) -> bool:
    """Refresh the channel's OAuth token if it is near expiry. Returns True if the channel is safe
    to publish, False if a refresh failure fenced it off (`connect_state=failed` + alert)."""
    expires_at = get_credential_expiry(session, product.id, credential_key, channel_id=channel.id)
    if not needs_refresh(expires_at, now):
        return True
    # A self-managed credential (structured blob, e.g. Reddit's PRAW kwargs) is refreshed by the
    # provider's own client — we hold no short-lived token of ours to replace, so proceed to publish
    # and let that client refresh under the hood. Only bare-token credentials are ours to refresh.
    current = get_credential(session, product.id, credential_key, channel_id=channel.id)
    if current and is_self_managed_credential(current):
        return True
    try:
        refresh(session, product, channel, now)
        return True
    except RefreshUnavailable:
        # No refresh handler configured for this provider — we can't proactively refresh, so proceed
        # and let the reactive AuthFailure fence catch the token if it's actually dead. Fencing here
        # would needlessly halt a channel whose token may still be valid.
        return True
    except Exception as exc:  # noqa: BLE001 — a real refresh failure fails the channel safe (S4.8)
        session.rollback()
        _fence_channel(session, channel, product, now, str(exc))
        return False


def publish_scheduled(
    session: Session, now: datetime, *, adapter_for=get_adapter, refresh=refresh_channel_token
) -> list[ContentItem]:
    """Publish every `scheduled` item whose time has come. Returns the items that went `published`.

    `adapter_for` and `refresh` are injectable so tests drive the full pass with no network,
    mirroring the `generate=`/`critique=` seam in the generate handler."""
    due = session.exec(
        select(ContentItem)
        .where(
            ContentItem.status == ContentItemStatus.SCHEDULED,
            col(ContentItem.scheduled_for).is_not(None),
            col(ContentItem.scheduled_for) <= now,
        )
        .order_by(col(ContentItem.scheduled_for), col(ContentItem.id))
    ).all()

    published: list[ContentItem] = []
    for item in due:
        channel = session.get(Channel, item.channel_id)
        # Kill switch / disabled / autonomy-off / dead-token (§7, S4.6, S4.8) checked immediately
        # before publish: skip and leave the item `scheduled` so it resumes once the channel
        # recovers. `autonomous` is re-checked here (not just at pace time) so turning autonomy off
        # after scheduling halts the publish too.
        if (
            channel is None
            or not channel.enabled
            or not channel.autonomous
            or channel.paused
            or channel.connect_state == ConnectState.FAILED
        ):
            continue
        product = session.get(Product, item.product_id)
        if product is None:  # orphaned item — permanent, don't retry forever
            item.status = ContentItemStatus.PUBLISH_FAILED
            item.error = f"product {item.product_id} not found"
            session.add(item)
            session.commit()
            continue

        try:
            adapter = adapter_for(channel.type)
            # Proactive OAuth refresh (S4.8): if this channel's token is within the refresh buffer
            # of expiry, refresh it before use. A failed refresh fails the channel safe — mark it
            # `failed`, fire an alert, and halt (leave the item `scheduled`) so a dead token never
            # silently kills the channel mid-window. Later due items then skip via the guard above.
            if adapter.credential_key and not _refresh_if_needed(
                session, product, channel, adapter.credential_key, now, refresh
            ):
                continue
            creds = (
                get_credential(session, product.id, adapter.credential_key, channel_id=channel.id)
                if adapter.credential_key
                else None
            )
            # Thread this item's UTM params onto any marketing-domain link in the body (S6.1) so
            # the published artifact and the stored body match, and a reader who clicks through is
            # attributable all the way to the funnel capture.
            item.body = thread_utm_links(item.body, product, channel, item)
            result = adapter.publish(item, product, channel, creds)
        except Retryable:
            # Transient — leave `scheduled`, retry next tick. Nothing was committed for this item.
            session.rollback()
            continue
        except AuthFailure as exc:
            # Dead/revoked token on a self-managed provider (S4.8): fence the whole channel and
            # leave the item `scheduled` so it resumes on reconnect — not a per-item publish_failed.
            session.rollback()
            _fence_channel(session, channel, product, now, str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 — permanent failure: record + move on (isolation)
            session.rollback()
            item.status = ContentItemStatus.PUBLISH_FAILED
            item.error = str(exc)
            session.add(item)
            session.commit()
            continue

        item.status = ContentItemStatus.PUBLISHED
        item.external_url = result.external_url
        item.published_at = now
        item.error = None
        session.add(item)
        # Per-item impression metric (reach comes later, S6.2+; attribution is the UTM thread above
        # + the S6.1 webhook/rollup join). Unique `source` makes the metric idempotent alongside the
        # item's status guard.
        session.add(
            MetricEvent(
                product_id=product.id,
                channel_id=channel.id,
                content_item_id=item.id,
                stage=MetricStage.IMPRESSION,
                value=1,
                source=f"publish:{item.idempotency_key}",
            )
        )
        session.commit()
        published.append(item)

    return published
