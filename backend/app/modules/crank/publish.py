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

from app.channels.base import Retryable, get_adapter
from app.models import Channel, ContentItem, MetricEvent, MetricStage, Product
from app.models.content_item import ContentItemStatus
from app.modules.crank.crank import _cadence_seconds  # reuse the crank's cadence-window clamp
from app.secrets.vault import get_credential


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
        select(Channel).where(Channel.enabled, Channel.autonomous, ~Channel.paused)
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


def publish_scheduled(
    session: Session, now: datetime, *, adapter_for=get_adapter
) -> list[ContentItem]:
    """Publish every `scheduled` item whose time has come. Returns the items that went `published`.

    `adapter_for` is injectable so tests drive the full pass with a stub adapter (no network),
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
        # Kill switch / disabled / autonomy-off checked immediately before publish (§7, S4.6): skip
        # and leave the item `scheduled` so it resumes when the channel is re-enabled. `autonomous`
        # is re-checked here (not just at pace time) so turning autonomy off after scheduling halts
        # the publish too.
        if channel is None or not channel.enabled or not channel.autonomous or channel.paused:
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
            creds = (
                get_credential(session, product.id, adapter.credential_key, channel_id=channel.id)
                if adapter.credential_key
                else None
            )
            result = adapter.publish(item, product, channel, creds)
        except Retryable:
            # Transient — leave `scheduled`, retry next tick. Nothing was committed for this item.
            session.rollback()
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
        # Per-item metric seam (reach/attribution fill in during P6). Unique `source` makes the
        # metric idempotent alongside the item's status guard.
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
