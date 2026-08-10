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

from app.channels.base import has_platform_reach
from app.models import Channel, ContentItem, FunnelEvent, FunnelEventType, MetricEvent, MetricStage
from app.models.product import Product
from app.modules.metrics.utm import resolve_attribution

_Key = tuple[int | None, int | None]


def zero_metrics() -> dict[str, int]:
    """The zeroed per-attribution metric shape — rollup row values and the calendar's default.

    `reach` starts at 0 but may end up `None` on a row whose channel has no platform counter
    (see `funnel_rollup`): unmeasured, not zero.
    """
    return {"impressions": 0, "reach": 0, "visits": 0, "signups": 0, "paid": 0, "revenue_cents": 0}


def funnel_rollup(session: Session, product: Product) -> dict:
    """Stage totals + per-channel/content-item attribution rows for one product."""
    row_values: dict[_Key, dict[str, int]] = {}
    # `impressions` is how many items we published; `reach` is how many people the platforms say
    # saw them (S6.2.1/#79). They were the same number until real polling landed, which is exactly
    # why they are now reported side by side — a dashboard showing one publish count labelled
    # "impressions" told the operator nothing about whether anyone was reached.
    stages = {"impressions": 0, "reach": 0, "visits": 0, "signups": 0, "paid": 0}
    revenue_cents = 0

    metrics = session.exec(select(MetricEvent).where(MetricEvent.product_id == product.id)).all()
    for metric in metrics:
        key = (metric.channel_id, metric.content_item_id)
        values = row_values.setdefault(key, zero_metrics())
        if metric.stage == MetricStage.IMPRESSION:
            stages["impressions"] += metric.value
            values["impressions"] += metric.value
        elif metric.stage == MetricStage.REACH:
            # Deltas, so summing them is the all-time reach for this attribution key.
            stages["reach"] += metric.value
            values["reach"] += metric.value
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
        values = row_values.setdefault(key, zero_metrics())
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
                if not has_platform_reach(channel.type):
                    # Owned infra (blog, podcast) has no platform counter. Reporting 0 here would
                    # tell the operator "nobody saw your blog post" when the truth is "we never
                    # measured it" — the same conflation this whole story exists to remove, just
                    # relocated into the dashboard. `None` renders as "—" instead.
                    row["reach"] = None
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


def metrics_by_content_item(session: Session, product: Product) -> dict[int, dict[str, int | None]]:
    """Per-content-item slice of `funnel_rollup`: its attribution rows summed by
    `content_item_id`, so per-item readers (the S6.3 calendar) reuse the same join instead of
    re-deriving attribution. Channel-only and unattributed rows have no item to land on and are
    dropped."""
    per_item: dict[int, dict[str, int]] = {}
    for row in funnel_rollup(session, product)["rows"]:
        item_id = row["content_item_id"]
        if item_id is None:
            continue
        values = per_item.setdefault(item_id, zero_metrics())
        for field in values:
            if field == "reach":
                continue  # summed below — None is "unmeasured" and must not become 0
            values[field] += row[field]
        # An item belongs to exactly one channel, so its rows are either all measured or all not;
        # no row here can mix the two.
        if row["reach"] is None:
            values["reach"] = None
        elif values["reach"] is not None:
            values["reach"] += row["reach"]
    return per_item
