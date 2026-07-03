"""Heartbeat digest + alerts (TECH_SPEC §8.4, PRD FR-31, story S6.2).

The daily observability job that makes "unattended ≥2 weeks" verifiable: per product, roll up the
last 24h per channel (published / failed / reach), persist it as a `heartbeat_digest` row (the
operator's Flower replacement, read back by the private API), and fire alerts through the
`raise_alert` choke point on: repeated publish-fail, dead/expired OAuth token, or zero-reach over
a window (shadowban signal).

Counting semantics: `published` and `reach` are 24h flows (`published_at` / `occurred_at` fall in
the window). `failed` is a *stock* — items currently sitting in `publish_failed` — because
content_item has no failed_at, and an unresolved failure should keep surfacing daily anyway
(matches how the dead-token alert re-fires while `connect_state=failed` persists).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlmodel import Session, func, select

from app.config import settings
from app.integrations.email import send_digest
from app.models import (
    Channel,
    ContentItem,
    ContentItemStatus,
    HeartbeatDigest,
    MetricEvent,
    MetricStage,
)
from app.models.product import Product
from app.modules.alerts import raise_alert

DIGEST_WINDOW = timedelta(hours=24)


def _published_count(session: Session, channel_id: int, since: datetime, now: datetime) -> int:
    return session.exec(
        select(func.count())
        .select_from(ContentItem)
        .where(
            ContentItem.channel_id == channel_id,
            ContentItem.published_at > since,  # type: ignore[arg-type]
            ContentItem.published_at <= now,  # type: ignore[arg-type]
        )
    ).one()


def _failed_count(session: Session, channel_id: int) -> int:
    return session.exec(
        select(func.count())
        .select_from(ContentItem)
        .where(
            ContentItem.channel_id == channel_id,
            ContentItem.status == ContentItemStatus.PUBLISH_FAILED,
        )
    ).one()


def _reach(session: Session, channel_id: int, since: datetime, now: datetime) -> int:
    total = session.exec(
        select(func.coalesce(func.sum(MetricEvent.value), 0)).where(
            MetricEvent.channel_id == channel_id,
            MetricEvent.stage == MetricStage.IMPRESSION,
            MetricEvent.occurred_at > since,
            MetricEvent.occurred_at <= now,
        )
    ).one()
    return int(total)


def build_digest(session: Session, product: Product, now: datetime) -> dict:
    """Per-channel published/failed/reach rows for the trailing 24h window."""
    since = now - DIGEST_WINDOW
    channels = session.exec(
        select(Channel).where(Channel.product_id == product.id).order_by(Channel.id)
    ).all()
    rows = [
        {
            "channel_id": ch.id,
            "channel_type": ch.type.value,
            "published": _published_count(session, ch.id, since, now),
            "failed": _failed_count(session, ch.id),
            "reach": _reach(session, ch.id, since, now),
        }
        for ch in channels
    ]
    return {"channels": rows}


def evaluate_alerts(session: Session, product: Product, digest: dict, now: datetime) -> list[dict]:
    """Alert conditions from §8.4: repeated publish-fail, dead token, zero-reach (shadowban)."""
    alerts: list[dict] = []
    channels = {
        ch.id: ch
        for ch in session.exec(select(Channel).where(Channel.product_id == product.id)).all()
    }

    for row in digest["channels"]:
        channel = channels[row["channel_id"]]
        if row["failed"] >= settings.heartbeat_publish_fail_threshold:
            alerts.append(
                _alert(
                    row,
                    "repeated_publish_fail",
                    f"{row['failed']} items stuck in publish_failed on {row['channel_type']}",
                )
            )
        if channel.connect_state.value == "failed":
            alerts.append(
                _alert(
                    row,
                    "oauth_token_dead",
                    f"{row['channel_type']} OAuth token dead/expired; publishes halted",
                )
            )

    # Zero-reach uses its own (longer) window than the 24h digest: published within N days but
    # zero impressions over those N days — the shadowban signature.
    window_start = now - timedelta(days=settings.heartbeat_zero_reach_window_days)
    for row in digest["channels"]:
        published_in_window = session.exec(
            select(func.count())
            .select_from(ContentItem)
            .where(
                ContentItem.channel_id == row["channel_id"],
                ContentItem.published_at > window_start,  # type: ignore[arg-type]
                ContentItem.published_at <= now,  # type: ignore[arg-type]
            )
        ).one()
        if published_in_window == 0:
            continue
        if _reach(session, row["channel_id"], window_start, now) == 0:
            alerts.append(
                _alert(
                    row,
                    "zero_reach",
                    f"{row['channel_type']} published {published_in_window} item(s) over "
                    f"{settings.heartbeat_zero_reach_window_days}d with zero reach "
                    "(shadowban signal)",
                )
            )
    return alerts


def _alert(row: dict, kind: str, message: str) -> dict:
    return {
        "kind": kind,
        "message": message,
        "channel_id": row["channel_id"],
        "channel_type": row["channel_type"],
    }


def _already_ran_today(session: Session, product_id: int, now: datetime) -> bool:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    existing = session.exec(
        select(HeartbeatDigest.id).where(
            HeartbeatDigest.product_id == product_id,
            HeartbeatDigest.window_end >= day_start,
            HeartbeatDigest.window_end <= now,
        )
    ).first()
    return existing is not None


def run_heartbeat(session: Session, now: datetime) -> list[HeartbeatDigest]:
    """Build + persist today's digest for every product; fire alerts; email best-effort.

    Idempotent per UTC day: a product whose digest already exists for `now`'s day is skipped, so
    the cron tick can safely re-run after a restart without double-sending.
    """
    created: list[HeartbeatDigest] = []
    for product in session.exec(select(Product)).all():
        if _already_ran_today(session, product.id, now):
            continue

        digest = build_digest(session, product, now)
        alerts = evaluate_alerts(session, product, digest, now)
        row = HeartbeatDigest(
            product_id=product.id,
            window_start=now - DIGEST_WINDOW,
            window_end=now,
            digest_json=json.dumps(digest),
            alerts_json=json.dumps(alerts),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        created.append(row)

        for alert in alerts:
            raise_alert(
                alert["kind"],
                alert["message"],
                product_id=product.id,
                channel_id=alert["channel_id"],
            )
        if settings.alert_email_to:
            send_digest(settings.alert_email_to, product, digest, alerts)
    return created
