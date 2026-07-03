"""S6.3: content calendar API — every item across all statuses with per-item funnel metrics
(real DB, no mocking)."""

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
    ContentItemStatus,
    FunnelEvent,
    FunnelEventType,
    MetricEvent,
    MetricStage,
    Product,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

ITEM_FIELDS = {
    "id",
    "channel_id",
    "content_type",
    "title",
    "status",
    "spot_check",
    "critic_score",
    "scheduled_for",
    "published_at",
    "created_at",
    "external_url",
    "metrics",
}

ZERO_METRICS = {"impressions": 0, "visits": 0, "signups": 0, "paid": 0, "revenue_cents": 0}


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


def _make_channel(engine, pid: int, *, type_=ChannelType.REDDIT) -> int:
    with Session(engine) as s:
        c = Channel(product_id=pid, type=type_, enabled=True, autonomous=True)
        s.add(c)
        s.commit()
        return c.id


def _make_item(engine, pid: int, cid: int, **kwargs) -> int:
    with Session(engine) as s:
        item = ContentItem(
            product_id=pid, channel_id=cid, content_type="social", body="Body", **kwargs
        )
        s.add(item)
        s.commit()
        return item.id


def test_calendar_missing_product_404(ctx):
    c, _ = ctx
    assert c.get("/api/private/content/999/calendar").status_code == 404


def test_calendar_empty_product_returns_empty_list(ctx):
    c, engine = ctx
    pid = _make_product(engine)

    resp = c.get(f"/api/private/content/{pid}/calendar")

    assert resp.status_code == 200
    assert resp.json() == []


def test_calendar_returns_every_status(ctx):
    c, engine = ctx
    pid = _make_product(engine)
    cid = _make_channel(engine, pid)
    for status in ContentItemStatus:
        _make_item(engine, pid, cid, status=status)

    body = c.get(f"/api/private/content/{pid}/calendar").json()

    assert len(body) == len(ContentItemStatus)
    assert {i["status"] for i in body} == {s.value for s in ContentItemStatus}


def test_calendar_item_field_shape(ctx):
    c, engine = ctx
    pid = _make_product(engine)
    cid = _make_channel(engine, pid)
    iid = _make_item(
        engine,
        pid,
        cid,
        status=ContentItemStatus.PUBLISHED,
        title="Item One",
        spot_check=True,
        critic_score=0.9,
        scheduled_for=datetime(2026, 7, 1, 11, 0, tzinfo=UTC),
        published_at=NOW,
        external_url="https://reddit.example/post1",
    )

    body = c.get(f"/api/private/content/{pid}/calendar").json()

    assert len(body) == 1
    item = body[0]
    assert set(item.keys()) == ITEM_FIELDS
    assert item["id"] == iid
    assert item["channel_id"] == cid
    assert item["content_type"] == "social"
    assert item["title"] == "Item One"
    assert item["status"] == "published"
    assert item["spot_check"] is True
    assert item["critic_score"] == 0.9
    assert item["scheduled_for"].startswith("2026-07-01T11:00")
    assert item["published_at"].startswith("2026-07-01T12:00")
    assert item["created_at"].startswith("2026-")
    assert item["external_url"] == "https://reddit.example/post1"
    assert item["metrics"] == ZERO_METRICS


def test_calendar_metrics_join_and_zero_default(ctx):
    c, engine = ctx
    pid = _make_product(engine)
    cid = _make_channel(engine, pid)
    item1 = _make_item(engine, pid, cid, status=ContentItemStatus.PUBLISHED, published_at=NOW)
    item2 = _make_item(engine, pid, cid, status=ContentItemStatus.GENERATED)

    with Session(engine) as s:
        for _ in range(3):
            s.add(
                MetricEvent(
                    product_id=pid,
                    channel_id=cid,
                    content_item_id=item1,
                    stage=MetricStage.IMPRESSION,
                    value=1,
                )
            )
        s.add(
            MetricEvent(
                product_id=pid,
                channel_id=cid,
                content_item_id=item1,
                stage=MetricStage.PAID,
                value=5000,
                source="stripe:cs_1",
            )
        )
        utm_content = f"sme-{item1}"
        for _ in range(2):
            s.add(
                FunnelEvent(
                    product_id=pid, event_type=FunnelEventType.VISIT, utm_content=utm_content
                )
            )
        s.add(FunnelEvent(product_id=pid, event_type=FunnelEventType.LEAD, utm_content=utm_content))
        # channel-only attribution (utm_source fallback) — must not count toward any item
        s.add(FunnelEvent(product_id=pid, event_type=FunnelEventType.VISIT, utm_source="reddit"))
        s.commit()

    body = c.get(f"/api/private/content/{pid}/calendar").json()

    by_id = {i["id"]: i for i in body}
    assert by_id[item1]["metrics"] == {
        "impressions": 3,
        "visits": 2,
        "signups": 1,
        "paid": 1,
        "revenue_cents": 5000,
    }
    assert by_id[item2]["metrics"] == ZERO_METRICS


def test_calendar_ordered_newest_first_by_coalesced_date(ctx):
    """Order key is COALESCE(published_at, scheduled_for, created_at) descending: a published
    item sorts by published_at, a scheduled one by scheduled_for even when created later, and a
    draft falls back to created_at."""
    c, engine = ctx
    pid = _make_product(engine)
    cid = _make_channel(engine, pid)
    published = _make_item(
        engine,
        pid,
        cid,
        status=ContentItemStatus.PUBLISHED,
        published_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        created_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
    )
    scheduled = _make_item(
        engine,
        pid,
        cid,
        status=ContentItemStatus.SCHEDULED,
        scheduled_for=datetime(2026, 7, 1, 11, 0, tzinfo=UTC),
        created_at=datetime(2026, 7, 1, 13, 0, tzinfo=UTC),
    )
    draft = _make_item(
        engine,
        pid,
        cid,
        status=ContentItemStatus.GENERATED,
        created_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
    )

    body = c.get(f"/api/private/content/{pid}/calendar").json()

    assert [i["id"] for i in body] == [published, scheduled, draft]


def test_calendar_scoped_to_product(ctx):
    c, engine = ctx
    pid = _make_product(engine)
    other_pid = _make_product(engine, slug="other")
    cid = _make_channel(engine, pid)
    other_cid = _make_channel(engine, other_pid)
    mine = _make_item(engine, pid, cid)
    _make_item(engine, other_pid, other_cid)

    body = c.get(f"/api/private/content/{pid}/calendar").json()

    assert [i["id"] for i in body] == [mine]
