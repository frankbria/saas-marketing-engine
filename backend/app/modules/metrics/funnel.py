"""Per-product attributed funnel rollup (TECH_SPEC §6.6/§8, story S6.1).

Stage totals come straight off the two funnel tables — impressions/paid from `metric_event`
(written at publish time and by the Stripe webhook join), visits/signups from `funnel_event` (the
only table carrying UTM). Attribution rows group by `(channel_id, content_item_id)`: metric_event
rows already carry those columns; funnel_event rows resolve them via `resolve_attribution`, the
same join the webhook uses (`app/api/public/stripe.py::_attribute_paid_metric`), so both readers
agree on what "attributed" means. Events that resolve to neither a channel nor a content item roll
into a single `(None, None)` row.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models import Channel, ContentItem, FunnelEvent, FunnelEventType, MetricEvent, MetricStage
from app.models.product import Product
from app.modules.metrics.utm import resolve_attribution

_Key = tuple[int | None, int | None]


def _empty_row_values() -> dict[str, int]:
    return {"impressions": 0, "visits": 0, "signups": 0, "paid": 0, "revenue_cents": 0}


def funnel_rollup(session: Session, product: Product) -> dict:
    """Stage totals + per-channel/content-item attribution rows for one product."""
    row_values: dict[_Key, dict[str, int]] = {}
    stages = {"impressions": 0, "visits": 0, "signups": 0, "paid": 0}
    revenue_cents = 0

    metrics = session.exec(select(MetricEvent).where(MetricEvent.product_id == product.id)).all()
    for metric in metrics:
        key = (metric.channel_id, metric.content_item_id)
        values = row_values.setdefault(key, _empty_row_values())
        if metric.stage == MetricStage.IMPRESSION:
            stages["impressions"] += metric.value
            values["impressions"] += metric.value
        elif metric.stage == MetricStage.PAID:
            stages["paid"] += 1
            revenue_cents += metric.value
            values["paid"] += 1
            values["revenue_cents"] += metric.value

    funnel_events = session.exec(
        select(FunnelEvent).where(FunnelEvent.product_id == product.id)
    ).all()
    for event in funnel_events:
        key = resolve_attribution(session, product.id, event.utm_source, event.utm_content)
        values = row_values.setdefault(key, _empty_row_values())
        if event.event_type == FunnelEventType.VISIT:
            stages["visits"] += 1
            values["visits"] += 1
        elif event.event_type == FunnelEventType.LEAD:
            stages["signups"] += 1
            values["signups"] += 1

    attributed_rows: list[dict] = []
    unattributed_row: dict | None = None
    for (channel_id, content_item_id), values in row_values.items():
        row = {
            "channel_id": channel_id,
            "channel_type": None,
            "content_item_id": content_item_id,
            "title": None,
            "external_url": None,
            **values,
        }
        # Ownership re-check on hydration: metric_event's channel/content ids have no FK, so a
        # malformed/backfilled row could point at another product — never expose its metadata here.
        if channel_id is not None:
            channel = session.get(Channel, channel_id)
            if channel is not None and channel.product_id == product.id:
                row["channel_type"] = channel.type.value
        if content_item_id is not None:
            content_item = session.get(ContentItem, content_item_id)
            if content_item is not None and content_item.product_id == product.id:
                row["title"] = content_item.title
                row["external_url"] = content_item.external_url

        if channel_id is None and content_item_id is None:
            unattributed_row = row
        else:
            attributed_rows.append(row)

    attributed_rows.sort(key=lambda r: (-r["revenue_cents"], -r["impressions"]))
    if unattributed_row is not None:
        attributed_rows.append(unattributed_row)

    return {"stages": stages, "revenue_cents": revenue_cents, "rows": attributed_rows}
