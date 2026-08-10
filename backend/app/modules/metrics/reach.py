"""Reach poll pass (TECH_SPEC §8.4, PRD FR-29/FR-31, story S6.2.1 / issue #79).

Polls each platform for the real engagement its published items earned and records the result as
`MetricStage.REACH` rows. Before this pass, "reach" was the publish counter — `publish_scheduled`
wrote one `IMPRESSION` row per published item and the heartbeat summed those same rows back, so
`published_in_window > 0` guaranteed `reach >= 1` and the zero-reach shadowban alert could never
fire. The two stages stay strictly apart: IMPRESSION answers "how many did we post", REACH answers
"how many did anyone see".

**Cumulative → delta.** Platforms report a running total; `metric_event` is append-only and summed
over a window. Each poll therefore records `cumulative - (everything already recorded for the
item)`, so the window sum reads as "reach gained during this window" — literally the question the
shadowban alert asks — while the all-time sum still equals the platform's own number.

Shaped like `publish_scheduled`: `now` injected, one bounded query, per-item `try`/`except` with a
per-item commit, and the same channel guard (§8.3 crash isolation). It never raises — a metrics
pass that can take down the scheduler tick would cost the operator the very observability it exists
to provide.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import Session, col, func, select

from app.channels.base import AuthFailure, Retryable, get_adapter
from app.config import settings
from app.models import Channel, ContentItem, MetricEvent, MetricStage, Product
from app.models.channel import ConnectState
from app.models.content_item import ContentItemStatus
from app.secrets.vault import get_credential

logger = logging.getLogger(__name__)


def _recorded_reach(session: Session, content_item_id: int) -> int:
    """Everything already recorded for this item, over all time — not just the alert window.

    All-time is required for the subtraction to be right: the platform's counter is cumulative
    since publication, so diffing it against a *windowed* sum would re-record every view older than
    the window on every tick.
    """
    total = session.exec(
        select(func.coalesce(func.sum(MetricEvent.value), 0)).where(
            MetricEvent.content_item_id == content_item_id,
            MetricEvent.stage == MetricStage.REACH,
        )
    ).one()
    return int(total)


def poll_reach(session: Session, now: datetime, *, adapter_for=get_adapter) -> list[MetricEvent]:
    """Poll every eligible published item for its platform counter; write the deltas.

    Returns the rows written. `adapter_for` is injectable so tests drive the whole pass with no
    network, mirroring the `adapter_for=`/`refresh=` seam in `publish_scheduled`.
    """
    # Bounded to the zero-reach alert's own window: polling wider would burn API quota on posts no
    # alert will ever read, and polling narrower would leave the alert reading a window this pass
    # never filled. One knob keeps the two from drifting apart.
    window_start = now - timedelta(days=settings.heartbeat_zero_reach_window_days)
    due = session.exec(
        select(ContentItem)
        .where(
            ContentItem.status == ContentItemStatus.PUBLISHED,
            col(ContentItem.published_at).is_not(None),
            col(ContentItem.published_at) > window_start,
            col(ContentItem.published_at) <= now,
        )
        .order_by(col(ContentItem.id))
    ).all()

    written: list[MetricEvent] = []
    for item in due:
        channel = session.get(Channel, item.channel_id)
        # Same guard as the publish pass: a disabled, paused, human-assisted or token-fenced
        # channel goes quiet here too. Polling a fenced channel would spend quota on credentials
        # already known to be dead, once per item, every tick.
        if (
            channel is None
            or not channel.enabled
            or not channel.autonomous
            or channel.paused
            or channel.connect_state == ConnectState.FAILED
        ):
            continue
        product = session.get(Product, item.product_id)
        if product is None:  # orphaned item — nothing to attribute the metric to
            continue

        try:
            try:
                adapter = adapter_for(channel.type)
            except LookupError:
                # No v1 adapter for this channel type (x, instagram — enabled but human-assisted).
                continue
            if not adapter.has_platform_reach:
                # Owned infra (blog, podcast): no third party to poll, and nothing that could
                # shadowban us. Skipping here is what makes the heartbeat's owned-channel exclusion
                # honest rather than "we looked and found nothing".
                continue
            creds = (
                get_credential(session, product.id, adapter.credential_key, channel_id=channel.id)
                if adapter.credential_key
                else None
            )
            if adapter.credential_key and creds is None:
                continue  # channel not connected yet — no-op rather than a loud per-tick failure
            cumulative = adapter.fetch_reach(item, product, channel, creds)
        except (Retryable, AuthFailure) as exc:
            # Expected, already-classified failures. A dead token is surfaced by the heartbeat's
            # `oauth_token_dead` alert off `connect_state`; the read path deliberately does not
            # fence the channel, because losing a metrics poll is not evidence a publish would fail.
            logger.info("reach poll skipped item %s: %s", item.id, exc)
            continue
        except Exception:
            logger.exception("reach poll failed for content_item %s", item.id)
            continue

        if cumulative is None:
            continue  # no number available this tick — not the same as a real zero
        # ponytail: read-then-insert is not locked. Two overlapping polls of the same item would
        # each diff against the same prior total and double-count — but they cannot overlap here:
        # APScheduler runs a job with max_instances=1, the app is a single uvicorn process, and
        # SQLite is single-writer (TECH_SPEC §4). Revisit if the poll ever moves onto Celery or a
        # second worker: the fix is a per-item row lock around the diff, which Postgres can do and
        # SQLite cannot.
        # A counter can move backwards (deleted comments, vote corrections, a platform reset). Clamp
        # rather than writing a negative delta, which would subtract real reach out of the window
        # and could manufacture the very zero-reach alert this pass exists to make trustworthy.
        delta = max(0, cumulative - _recorded_reach(session, item.id))
        if delta == 0:
            continue  # nothing new: a zero-value row would add nothing to any sum

        row = MetricEvent(
            product_id=product.id,
            channel_id=channel.id,
            content_item_id=item.id,
            stage=MetricStage.REACH,
            value=delta,
            occurred_at=now,
            # Unique per (item, tick): makes a re-run of the same tick a constraint violation
            # instead of a double-count — the idempotency contract the publish + Stripe rows use.
            source=f"reach:{item.id}:{now.isoformat()}",
        )
        try:
            session.add(row)
            session.commit()
        except Exception:
            # Per-item commit + rollback so one bad row (e.g. a source collision from a re-run
            # tick) never discards the deltas already banked for its siblings.
            session.rollback()
            logger.exception("reach poll could not record delta for content_item %s", item.id)
            continue
        written.append(row)

    return written
