"""S6.2.1: real reach ingestion (issue #79).

`poll_reach` turns each platform's **cumulative** engagement counter into the **delta** rows
`metric_event` is built for. That conversion is the whole point of the pass: the table is
append-only and every reader sums it over a window, so writing the gauge itself would re-count the
same views on every tick and make `sum(value) over 7 days` mean nothing.

These tests drive the pass with a fake adapter — no network, no credentials — because the seam
under test is the poll's arithmetic and isolation, not PRAW/httpx wire format (those are covered by
the adapter tests). The channel-eligibility cases mirror `publish_scheduled`'s guard: a paused or
fenced channel must go quiet here too, or a dead channel would keep burning API quota.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, func, select

from app.channels.base import AuthFailure, PublishResult, Retryable
from app.config import settings
from app.models import (
    Channel,
    ChannelType,
    ContentItem,
    ContentItemStatus,
    MetricEvent,
    MetricStage,
    Product,
)
from app.models.channel import ConnectState
from app.modules.crank.publish import publish_scheduled
from app.modules.heartbeat import build_digest, evaluate_alerts
from app.modules.metrics.reach import poll_reach

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(eng, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(eng)
    return eng


class FakeAdapter:
    """Stands in for a platform adapter. `counts` maps external_url -> cumulative count; a value
    that is an Exception instance is raised instead, to drive the isolation cases."""

    credential_key = None
    has_platform_reach = True

    def __init__(self, counts: dict[str, object]):
        self.counts = counts
        self.calls: list[str] = []

    def fetch_reach(self, item, product, channel, creds) -> int | None:
        self.calls.append(item.external_url)
        value = self.counts.get(item.external_url)
        if isinstance(value, Exception):
            raise value
        return value  # type: ignore[return-value]


class OwnedAdapter:
    credential_key = None
    has_platform_reach = False

    def __init__(self):
        self.calls: list[str] = []

    def fetch_reach(self, item, product, channel, creds) -> int | None:
        self.calls.append(item.external_url)
        return None


def _product(session: Session) -> Product:
    product = Product(name="Acme", slug="acme")
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def _channel(session: Session, product_id: int, type_=ChannelType.REDDIT, **kwargs) -> Channel:
    channel = Channel(
        product_id=product_id,
        type=type_,
        enabled=kwargs.pop("enabled", True),
        autonomous=kwargs.pop("autonomous", True),
        paused=kwargs.pop("paused", False),
        connect_state=kwargs.pop("connect_state", ConnectState.CONNECTED),
    )
    session.add(channel)
    session.commit()
    session.refresh(channel)
    return channel


def _item(
    session: Session, product_id: int, channel_id: int, url: str, *, age_days=1
) -> ContentItem:
    item = ContentItem(
        product_id=product_id,
        channel_id=channel_id,
        content_type="social",
        title="post",
        body="body",
        status=ContentItemStatus.PUBLISHED,
        external_url=url,
        published_at=NOW - timedelta(days=age_days),
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def _reach_total(session: Session, channel_id: int) -> int:
    return int(
        session.exec(
            select(func.coalesce(func.sum(MetricEvent.value), 0)).where(
                MetricEvent.channel_id == channel_id,
                MetricEvent.stage == MetricStage.REACH,
            )
        ).one()
    )


def _reach_rows(session: Session) -> list[MetricEvent]:
    return list(
        session.exec(select(MetricEvent).where(MetricEvent.stage == MetricStage.REACH)).all()
    )


# --- the cumulative → delta conversion ---------------------------------------


def test_first_poll_records_the_whole_cumulative_count_as_one_delta(engine):
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id)
        _item(s, product.id, channel.id, "https://reddit.test/a")
        adapter = FakeAdapter({"https://reddit.test/a": 10})

        poll_reach(s, NOW, adapter_for=lambda _t: adapter)

        rows = _reach_rows(s)
        assert [r.value for r in rows] == [10]
        assert rows[0].stage == MetricStage.REACH
        assert rows[0].content_item_id is not None


def test_second_poll_records_only_the_increase(engine):
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id)
        _item(s, product.id, channel.id, "https://reddit.test/a")

        poll_reach(s, NOW, adapter_for=lambda _t: FakeAdapter({"https://reddit.test/a": 10}))
        poll_reach(
            s,
            NOW + timedelta(hours=1),
            adapter_for=lambda _t: FakeAdapter({"https://reddit.test/a": 14}),
        )

        assert sorted(r.value for r in _reach_rows(s)) == [4, 10]
        # The windowed sum every reader uses must equal the platform's cumulative number.
        assert _reach_total(s, channel.id) == 14


def test_counter_going_backwards_writes_nothing_rather_than_a_negative_delta(engine):
    """A deleted comment, a vote correction, or a platform-side reset can lower the counter.
    A negative delta would silently *subtract* real reach from the window and could push a healthy
    channel's sum to zero — manufacturing the exact shadowban alert this feature exists to make
    trustworthy."""
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id)
        _item(s, product.id, channel.id, "https://reddit.test/a")

        poll_reach(s, NOW, adapter_for=lambda _t: FakeAdapter({"https://reddit.test/a": 14}))
        poll_reach(
            s,
            NOW + timedelta(hours=1),
            adapter_for=lambda _t: FakeAdapter({"https://reddit.test/a": 3}),
        )

        assert [r.value for r in _reach_rows(s)] == [14]
        assert _reach_total(s, channel.id) == 14


def test_unchanged_counter_writes_no_row(engine):
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id)
        _item(s, product.id, channel.id, "https://reddit.test/a")

        poll_reach(s, NOW, adapter_for=lambda _t: FakeAdapter({"https://reddit.test/a": 10}))
        poll_reach(
            s,
            NOW + timedelta(hours=1),
            adapter_for=lambda _t: FakeAdapter({"https://reddit.test/a": 10}),
        )

        assert [r.value for r in _reach_rows(s)] == [10]


def test_zero_cumulative_writes_no_row_so_the_window_sum_stays_zero(engine):
    """The headline case: a published post nobody saw. No row is the correct representation —
    `_reach()` sums to 0 and the shadowban alert fires."""
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id)
        _item(s, product.id, channel.id, "https://reddit.test/a")

        poll_reach(s, NOW, adapter_for=lambda _t: FakeAdapter({"https://reddit.test/a": 0}))

        assert _reach_rows(s) == []
        assert _reach_total(s, channel.id) == 0


def test_none_from_the_adapter_writes_no_row(engine):
    """None is "no number this tick" (deleted post, drifted payload) — distinct from a real zero,
    and it must not be recorded as one."""
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id)
        _item(s, product.id, channel.id, "https://reddit.test/a")

        poll_reach(s, NOW, adapter_for=lambda _t: FakeAdapter({"https://reddit.test/a": None}))

        assert _reach_rows(s) == []


# --- what gets polled at all -------------------------------------------------


def test_owned_channels_are_never_polled(engine):
    """Blog/podcast have no platform counter; the poll must not even ask."""
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id, ChannelType.BLOG)
        _item(s, product.id, channel.id, "https://acme.test/blog/a")
        adapter = OwnedAdapter()

        poll_reach(s, NOW, adapter_for=lambda _t: adapter)

        assert adapter.calls == []
        assert _reach_rows(s) == []


def test_items_published_outside_the_window_are_not_polled(engine):
    """The bound that keeps API quota finite: an ever-growing back catalogue would otherwise be
    re-polled forever."""
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id)
        stale = settings.heartbeat_zero_reach_window_days + 1
        _item(s, product.id, channel.id, "https://reddit.test/old", age_days=stale)
        _item(s, product.id, channel.id, "https://reddit.test/new", age_days=1)
        adapter = FakeAdapter({"https://reddit.test/old": 99, "https://reddit.test/new": 5})

        poll_reach(s, NOW, adapter_for=lambda _t: adapter)

        assert adapter.calls == ["https://reddit.test/new"]


def test_unpublished_items_are_not_polled(engine):
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id)
        item = _item(s, product.id, channel.id, "https://reddit.test/a")
        item.status = ContentItemStatus.SCHEDULED
        s.add(item)
        s.commit()
        adapter = FakeAdapter({"https://reddit.test/a": 10})

        poll_reach(s, NOW, adapter_for=lambda _t: adapter)

        assert adapter.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"enabled": False},
        {"autonomous": False},
        {"paused": True},
        {"connect_state": ConnectState.FAILED},
    ],
    ids=["disabled", "not-autonomous", "paused", "token-fenced"],
)
def test_ineligible_channels_are_not_polled(engine, kwargs):
    """Same guard as `publish_scheduled`: a channel that is off must not spend API quota."""
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id, **kwargs)
        _item(s, product.id, channel.id, "https://reddit.test/a")
        adapter = FakeAdapter({"https://reddit.test/a": 10})

        poll_reach(s, NOW, adapter_for=lambda _t: adapter)

        assert adapter.calls == []
        assert _reach_rows(s) == []


# --- isolation ---------------------------------------------------------------


def test_one_failing_item_never_stops_the_pass(engine):
    """Per-item try/except, mirroring `publish_scheduled` (§8.3 crash isolation): a single dead
    post must not cost every other channel its metrics for the tick."""
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id)
        _item(s, product.id, channel.id, "https://reddit.test/boom")
        _item(s, product.id, channel.id, "https://reddit.test/ok")
        adapter = FakeAdapter(
            {
                "https://reddit.test/boom": RuntimeError("reddit exploded"),
                "https://reddit.test/ok": 7,
            }
        )

        poll_reach(s, NOW, adapter_for=lambda _t: adapter)  # must not raise

        assert [r.value for r in _reach_rows(s)] == [7]


def test_a_channel_with_no_adapter_is_skipped(engine):
    """X/Instagram are enabled-but-human-assisted and have no v1 adapter; `get_adapter` raises for
    them. The poll degrades to a no-op instead of taking the whole tick down."""

    def _no_adapter(_type):
        raise LookupError("no v1 publish adapter")

    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id, ChannelType.X)
        _item(s, product.id, channel.id, "https://x.test/a")

        poll_reach(s, NOW, adapter_for=_no_adapter)  # must not raise

        assert _reach_rows(s) == []


# --- the alert this whole story exists for -----------------------------------


class PublishingFakeAdapter(FakeAdapter):
    """A fake that both publishes and reports reach, so one test can cross the seam where the
    original defect lived: each half was individually correct, only their overlap was wrong."""

    def publish(self, item, product, channel, creds) -> PublishResult:
        return PublishResult(external_url=f"https://reddit.test/{item.id}")


def _scheduled_item(session: Session, product_id: int, channel_id: int) -> ContentItem:
    item = ContentItem(
        product_id=product_id,
        channel_id=channel_id,
        content_type="social",
        title="post",
        body="body",
        status=ContentItemStatus.SCHEDULED,
        scheduled_for=NOW - timedelta(minutes=1),
        idempotency_key=f"reddit:{channel_id}:1",
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def _publish_poll_and_evaluate(session: Session, product: Product, cumulative: int) -> list[str]:
    """Drive the real pipeline end to end and return the alert kinds it produced."""
    adapter = PublishingFakeAdapter({})
    publish_scheduled(session, NOW, adapter_for=lambda _t: adapter)
    published = session.exec(
        select(ContentItem).where(ContentItem.status == ContentItemStatus.PUBLISHED)
    ).all()
    assert published, "precondition: the publish pass must have published the item"

    adapter.counts = {item.external_url: cumulative for item in published}
    poll_reach(session, NOW + timedelta(minutes=1), adapter_for=lambda _t: adapter)

    later = NOW + timedelta(minutes=2)
    digest = build_digest(session, product, later)
    return [a["kind"] for a in evaluate_alerts(session, product, digest, later)]


def test_zero_reach_alert_fires_for_a_published_post_nobody_saw(engine):
    """The headline criterion of #79 — and the assertion that was impossible before it.

    Publishing writes an `PUBLISHED` row. While `_reach()` summed `PUBLISHED`, that row *was*
    the reach, so `published_in_window > 0` implied `reach >= 1` and this alert could never fire,
    no matter how invisible the post was. Now publishing and reach are different stages: the post
    is live, the platform reports zero engagement, and the shadowban signal gets through.
    """
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id)
        _scheduled_item(s, product.id, channel.id)

        assert _publish_poll_and_evaluate(s, product, cumulative=0) == ["zero_reach"]

        # ...and the publish counter is still intact and still separate — the fix separates the
        # two stages rather than trading one broken number for another.
        published_rows = s.exec(
            select(MetricEvent).where(MetricEvent.stage == MetricStage.PUBLISHED)
        ).all()
        assert len(published_rows) == 1
        assert _reach_rows(s) == []


def test_no_zero_reach_alert_when_the_platform_reports_real_engagement(engine):
    """The other half of the proof: the alert is sensitive to real data, not firing unconditionally.
    A test that only pins the firing case would pass just as well against `alert_always()`."""
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id)
        _scheduled_item(s, product.id, channel.id)

        assert _publish_poll_and_evaluate(s, product, cumulative=12) == []
        assert [r.value for r in _reach_rows(s)] == [12]


# --- degrade-to-no-op paths --------------------------------------------------


class CredentialedFakeAdapter(FakeAdapter):
    credential_key = "reddit_oauth"


def test_channel_without_a_stored_credential_is_not_polled(engine):
    """ "Degrades to a no-op on an unconfigured channel" (issue #79): a channel connected in the UI
    but with nothing in the vault yet must go quiet, not raise a per-item error every tick."""
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id)
        _item(s, product.id, channel.id, "https://reddit.test/a")
        adapter = CredentialedFakeAdapter({"https://reddit.test/a": 10})

        poll_reach(s, NOW, adapter_for=lambda _t: adapter)

        assert adapter.calls == []
        assert _reach_rows(s) == []


@pytest.mark.parametrize(
    "error",
    [Retryable("rate limited"), AuthFailure("token revoked")],
    ids=["retryable", "auth-failure"],
)
def test_classified_adapter_failures_write_no_row(engine, error):
    """A rate-limit or a dead token means "no number", never "zero reach" — recording a zero here
    would fire a shadowban alert on a channel whose posts may be doing fine."""
    with Session(engine) as s:
        product = _product(s)
        channel = _channel(s, product.id)
        _item(s, product.id, channel.id, "https://reddit.test/a")

        poll_reach(s, NOW, adapter_for=lambda _t: FakeAdapter({"https://reddit.test/a": error}))

        assert _reach_rows(s) == []
        # The read path must not fence the channel: losing a metrics poll is not evidence that a
        # publish would fail, and fencing here would halt publishing on a rate-limit blip.
        s.refresh(channel)
        assert channel.connect_state == ConnectState.CONNECTED
