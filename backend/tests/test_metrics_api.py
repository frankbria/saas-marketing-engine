"""S6.1: funnel rollup API — stage totals + attribution rows (real DB, no mocking)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session
from app.main import create_app
from app.models import (
    Channel,
    ChannelType,
    ContentItem,
    FunnelEvent,
    FunnelEventType,
    MetricEvent,
    MetricStage,
    Product,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def ctx(tmp_path):
    db = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(engine)

    def _session_override():
        with Session(engine) as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _session_override
    with TestClient(app) as c:
        yield c, engine


def _make_product(engine, *, slug="acme") -> int:
    with Session(engine) as s:
        p = Product(name=slug, slug=slug, marketing_domain="acme.example")
        s.add(p)
        s.commit()
        return p.id


def test_funnel_missing_product_404(ctx):
    c, _ = ctx
    assert c.get("/api/private/metrics/999/funnel").status_code == 404


def test_funnel_empty_product_returns_zeros(ctx):
    c, engine = ctx
    pid = _make_product(engine)

    resp = c.get(f"/api/private/metrics/{pid}/funnel")

    assert resp.status_code == 200
    assert resp.json() == {
        "stages": {"impressions": 0, "visits": 0, "signups": 0, "paid": 0},
        "revenue_cents": 0,
        "rows": [],
    }


def _seed_scenario(engine, pid: int) -> dict:
    with Session(engine) as s:
        channel_a = Channel(product_id=pid, type=ChannelType.REDDIT, enabled=True, autonomous=True)
        channel_b = Channel(product_id=pid, type=ChannelType.BLOG, enabled=True, autonomous=True)
        s.add(channel_a)
        s.add(channel_b)
        s.commit()
        s.refresh(channel_a)
        s.refresh(channel_b)

        item1 = ContentItem(
            product_id=pid,
            channel_id=channel_a.id,
            content_type="social",
            title="Item One",
            body="Body one",
            external_url="https://acme.example/item1",
        )
        item2 = ContentItem(
            product_id=pid,
            channel_id=channel_b.id,
            content_type="blog",
            title="Item Two",
            body="Body two",
        )
        s.add(item1)
        s.add(item2)
        s.commit()
        s.refresh(item1)
        s.refresh(item2)

        # impressions: 3 on item1/channel_a, 1 on item2/channel_b
        for _ in range(3):
            s.add(
                MetricEvent(
                    product_id=pid,
                    channel_id=channel_a.id,
                    content_item_id=item1.id,
                    stage=MetricStage.IMPRESSION,
                    value=1,
                )
            )
        s.add(
            MetricEvent(
                product_id=pid,
                channel_id=channel_b.id,
                content_item_id=item2.id,
                stage=MetricStage.IMPRESSION,
                value=1,
            )
        )

        # paid: one fully attributed to item1/channel_a, one unattributable
        s.add(
            MetricEvent(
                product_id=pid,
                channel_id=channel_a.id,
                content_item_id=item1.id,
                stage=MetricStage.PAID,
                value=5000,
                source="stripe:cs_1",
            )
        )
        s.add(
            MetricEvent(
                product_id=pid,
                stage=MetricStage.PAID,
                value=1000,
                source="stripe:cs_2",
            )
        )

        # visits/signups via funnel_event UTM resolution
        utm_content_item1 = f"sme-{item1.id}"
        s.add(
            FunnelEvent(
                product_id=pid, event_type=FunnelEventType.VISIT, utm_content=utm_content_item1
            )
        )
        s.add(
            FunnelEvent(
                product_id=pid, event_type=FunnelEventType.VISIT, utm_content=utm_content_item1
            )
        )
        s.add(
            FunnelEvent(
                product_id=pid, event_type=FunnelEventType.LEAD, utm_content=utm_content_item1
            )
        )
        # utm_source-only fallback -> channel_b, no content item
        s.add(FunnelEvent(product_id=pid, event_type=FunnelEventType.VISIT, utm_source="blog"))
        # no UTM at all -> unattributed
        s.add(FunnelEvent(product_id=pid, event_type=FunnelEventType.VISIT))

        s.commit()
        return {
            "channel_a": channel_a.id,
            "channel_b": channel_b.id,
            "item1": item1.id,
            "item2": item2.id,
        }


def test_funnel_seeded_scenario_stage_totals_and_rows(ctx):
    c, engine = ctx
    pid = _make_product(engine)
    ids = _seed_scenario(engine, pid)

    resp = c.get(f"/api/private/metrics/{pid}/funnel")
    assert resp.status_code == 200
    body = resp.json()

    assert body["stages"] == {"impressions": 4, "visits": 4, "signups": 1, "paid": 2}
    assert body["revenue_cents"] == 6000

    rows = body["rows"]
    assert len(rows) == 4

    row_item1 = rows[0]
    assert row_item1["channel_id"] == ids["channel_a"]
    assert row_item1["channel_type"] == "reddit"
    assert row_item1["content_item_id"] == ids["item1"]
    assert row_item1["title"] == "Item One"
    assert row_item1["external_url"] == "https://acme.example/item1"
    assert row_item1["impressions"] == 3
    assert row_item1["visits"] == 2
    assert row_item1["signups"] == 1
    assert row_item1["paid"] == 1
    assert row_item1["revenue_cents"] == 5000

    row_item2 = rows[1]
    assert row_item2["channel_id"] == ids["channel_b"]
    assert row_item2["channel_type"] == "blog"
    assert row_item2["content_item_id"] == ids["item2"]
    assert row_item2["title"] == "Item Two"
    assert row_item2["external_url"] is None
    assert row_item2["impressions"] == 1
    assert row_item2["visits"] == 0
    assert row_item2["signups"] == 0
    assert row_item2["paid"] == 0
    assert row_item2["revenue_cents"] == 0

    row_channel_only = rows[2]
    assert row_channel_only["channel_id"] == ids["channel_b"]
    assert row_channel_only["channel_type"] == "blog"
    assert row_channel_only["content_item_id"] is None
    assert row_channel_only["title"] is None
    assert row_channel_only["external_url"] is None
    assert row_channel_only["visits"] == 1
    assert row_channel_only["impressions"] == 0

    # unattributed row always last
    row_unattributed = rows[3]
    assert row_unattributed["channel_id"] is None
    assert row_unattributed["channel_type"] is None
    assert row_unattributed["content_item_id"] is None
    assert row_unattributed["title"] is None
    assert row_unattributed["external_url"] is None
    assert row_unattributed["visits"] == 1
    assert row_unattributed["paid"] == 1
    assert row_unattributed["revenue_cents"] == 1000
