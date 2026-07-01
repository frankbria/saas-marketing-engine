"""S4.5: publish adapters (blog + Reddit) with idempotency + pacing.

Drives the two pure passes (`pace_content`, `publish_scheduled`) and the adapters directly against a
real SQLite file — deterministic, no scheduler thread, `now` injected so pacing/dueness are
controllable. Network side effects are injected (stub adapter / fake praw client), matching the
`generate=`/`critique=` seam house style — no mocking of the code under test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels.base import PublishResult, Retryable, get_adapter
from app.channels.blog import BlogAdapter
from app.channels.reddit import RedditAdapter
from app.models import (
    Channel,
    ChannelType,
    ContentItem,
    ContentItemStatus,
    MetricEvent,
    MetricStage,
    Product,
)
from app.modules.crank.crank import WEEKLY_SECONDS
from app.modules.crank.publish import pace_content, publish_scheduled

NOW = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)


def _utc(dt):
    """SQLite hands back tz-naive datetimes; normalize a DB-read value to aware UTC for asserts."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@pytest.fixture
def session(tmp_path):
    db = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _product(session, *, slug="acme", domain="acme.example"):
    p = Product(name=slug, slug=slug, marketing_domain=domain)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _channel(
    session,
    product_id,
    *,
    ctype=ChannelType.BLOG,
    daily_cap=None,
    paused=False,
    enabled=True,
    autonomous=True,
    profile=None,
):
    c = Channel(
        product_id=product_id,
        type=ctype,
        enabled=enabled,
        autonomous=autonomous,
        paused=paused,
        daily_cap=daily_cap,
        profile_json=json.dumps(profile) if profile else None,
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _item(
    session,
    product_id,
    channel_id,
    *,
    status=ContentItemStatus.CRITIC_PASSED,
    title="Title",
    body="Body copy",
    meta=None,
    created=NOW,
):
    it = ContentItem(
        product_id=product_id,
        channel_id=channel_id,
        content_type="blog",
        status=status,
        title=title,
        body=body,
        meta_json=json.dumps(meta) if meta else None,
        created_at=created,
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


class StubAdapter:
    """Records publish calls and returns a canned URL (or raises an injected error)."""

    def __init__(self, *, credential_key=None, error=None, url="https://acme.example/blog/x"):
        self.credential_key = credential_key
        self.error = error
        self.url = url
        self.calls: list[int] = []

    def publish(self, item, product, channel, creds):
        self.calls.append(item.id)
        if self.error is not None:
            raise self.error
        return PublishResult(external_url=self.url)

    def delete(self, external_url, product, channel, creds):  # pragma: no cover - unused here
        pass


# --- pacing (AC3) ------------------------------------------------------------------------------


def test_pace_schedules_critic_passed_with_key_and_spread(session):
    p = _product(session)
    c = _channel(session, p.id, daily_cap=7)  # weekly window / 7 => 1 day spacing
    items = [_item(session, p.id, c.id, created=NOW + timedelta(seconds=i)) for i in range(3)]

    scheduled = pace_content(session, NOW)

    assert {i.id for i in scheduled} == {i.id for i in items}
    rows = session.exec(select(ContentItem).order_by(ContentItem.scheduled_for)).all()
    assert all(r.status == ContentItemStatus.SCHEDULED for r in rows)
    assert [r.idempotency_key for r in rows] == [f"blog:{i.id}" for i in items]
    # spread one day apart, first at now
    assert _utc(rows[0].scheduled_for) == NOW
    assert _utc(rows[1].scheduled_for) == NOW + timedelta(days=1)
    assert _utc(rows[2].scheduled_for) == NOW + timedelta(days=2)


def test_pace_respects_daily_cap_no_burst(session):
    p = _product(session)
    c = _channel(session, p.id, daily_cap=1)  # weekly / 1 => 7-day spacing
    for i in range(3):
        _item(session, p.id, c.id, created=NOW + timedelta(seconds=i))

    pace_content(session, NOW)

    times = sorted(r.scheduled_for for r in session.exec(select(ContentItem)).all())
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)]
    assert all(g >= 86400 for g in gaps)  # never more than one per day


def test_pace_unset_cap_spreads_batch_across_window(session):
    p = _product(session)
    c = _channel(session, p.id, daily_cap=None)  # spread batch across the weekly window
    for i in range(2):
        _item(session, p.id, c.id, created=NOW + timedelta(seconds=i))

    pace_content(session, NOW)
    times = sorted(r.scheduled_for for r in session.exec(select(ContentItem)).all())
    assert (times[1] - times[0]).total_seconds() == pytest.approx(WEEKLY_SECONDS / 2)


def test_pace_second_batch_keeps_spacing_past_first(session):
    p = _product(session)
    c = _channel(session, p.id, daily_cap=7)
    _item(session, p.id, c.id)
    pace_content(session, NOW)  # first item scheduled at NOW
    _item(session, p.id, c.id, created=NOW + timedelta(hours=1))
    pace_content(session, NOW)  # second batch must not reuse NOW

    times = sorted(_utc(r.scheduled_for) for r in session.exec(select(ContentItem)).all())
    assert times[0] == NOW
    assert times[1] == NOW + timedelta(days=1)


def test_pace_skips_paused_disabled_and_manual_channels(session):
    p = _product(session)
    paused = _channel(session, p.id, ctype=ChannelType.BLOG, paused=True)
    disabled = _channel(session, p.id, ctype=ChannelType.REDDIT, enabled=False)
    manual = _channel(session, p.id, ctype=ChannelType.X, autonomous=False)
    for c in (paused, disabled, manual):
        _item(session, p.id, c.id)

    assert pace_content(session, NOW) == []
    assert all(
        r.status == ContentItemStatus.CRITIC_PASSED for r in session.exec(select(ContentItem)).all()
    )


# --- publish (AC1, AC2, AC4) -------------------------------------------------------------------


def _scheduled_item(session, p, c, **kw):
    it = _item(session, p.id, c.id, status=ContentItemStatus.SCHEDULED, **kw)
    it.scheduled_for = NOW
    it.idempotency_key = f"{c.type.value}:{it.id}"
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def test_publish_due_item_records_result_and_metric(session):
    p = _product(session)
    c = _channel(session, p.id)
    it = _scheduled_item(session, p, c)
    stub = StubAdapter(url="https://acme.example/blog/hello")

    published = publish_scheduled(session, NOW, adapter_for=lambda t: stub)

    assert [i.id for i in published] == [it.id]
    session.refresh(it)
    assert it.status == ContentItemStatus.PUBLISHED
    assert it.external_url == "https://acme.example/blog/hello"
    assert _utc(it.published_at) == NOW
    metric = session.exec(select(MetricEvent)).one()
    assert metric.stage == MetricStage.IMPRESSION
    assert metric.content_item_id == it.id and metric.channel_id == c.id
    assert metric.source == f"publish:{it.idempotency_key}"


def test_publish_skips_not_yet_due(session):
    p = _product(session)
    c = _channel(session, p.id)
    it = _scheduled_item(session, p, c)
    it.scheduled_for = NOW + timedelta(hours=1)
    session.add(it)
    session.commit()

    assert publish_scheduled(session, NOW, adapter_for=lambda t: StubAdapter()) == []
    session.refresh(it)
    assert it.status == ContentItemStatus.SCHEDULED


def test_publish_idempotent_no_double_post(session):
    p = _product(session)
    c = _channel(session, p.id)
    _scheduled_item(session, p, c)
    stub = StubAdapter()

    publish_scheduled(session, NOW, adapter_for=lambda t: stub)
    publish_scheduled(session, NOW, adapter_for=lambda t: stub)  # second pass: already published

    assert len(stub.calls) == 1  # published exactly once


def test_publish_transient_failure_stays_scheduled(session):
    p = _product(session)
    c = _channel(session, p.id)
    it = _scheduled_item(session, p, c)
    stub = StubAdapter(error=Retryable("rate limited"))

    assert publish_scheduled(session, NOW, adapter_for=lambda t: stub) == []
    session.refresh(it)
    assert it.status == ContentItemStatus.SCHEDULED  # retried next tick
    assert session.exec(select(MetricEvent)).all() == []


def test_publish_permanent_failure_marks_failed(session):
    p = _product(session)
    c = _channel(session, p.id)
    it = _scheduled_item(session, p, c)
    stub = StubAdapter(error=RuntimeError("bad config"))

    assert publish_scheduled(session, NOW, adapter_for=lambda t: stub) == []
    session.refresh(it)
    assert it.status == ContentItemStatus.PUBLISH_FAILED
    assert it.error == "bad config"


def test_publish_paused_channel_kill_switch(session):
    p = _product(session)
    c = _channel(session, p.id)
    it = _scheduled_item(session, p, c)
    c.paused = True  # kill switch flipped after scheduling
    session.add(c)
    session.commit()
    stub = StubAdapter()

    assert publish_scheduled(session, NOW, adapter_for=lambda t: stub) == []
    assert stub.calls == []  # never called for a paused channel
    session.refresh(it)
    assert it.status == ContentItemStatus.SCHEDULED


def test_publish_one_failure_does_not_block_siblings(session):
    p = _product(session)
    c = _channel(session, p.id)
    bad = _scheduled_item(session, p, c, title="bad")
    good = _scheduled_item(session, p, c, title="good")

    def adapter_for(_t):
        return _SelectiveAdapter(fail_item_id=bad.id)

    published = publish_scheduled(session, NOW, adapter_for=adapter_for)
    assert [i.id for i in published] == [good.id]
    session.refresh(bad)
    session.refresh(good)
    assert bad.status == ContentItemStatus.PUBLISH_FAILED
    assert good.status == ContentItemStatus.PUBLISHED


class _SelectiveAdapter(StubAdapter):
    def __init__(self, *, fail_item_id):
        super().__init__()
        self.fail_item_id = fail_item_id

    def publish(self, item, product, channel, creds):
        if item.id == self.fail_item_id:
            raise RuntimeError("boom")
        return PublishResult(external_url=f"https://acme.example/blog/{item.id}")


# --- blog adapter (AC1) ------------------------------------------------------------------------


def test_blog_adapter_writes_file_and_is_idempotent(session, tmp_path, monkeypatch):
    monkeypatch.setattr("app.workspace.settings.workspace_root", str(tmp_path))
    p = _product(session, domain="acme.example")
    c = _channel(session, p.id)
    it = _item(
        session,
        p.id,
        c.id,
        title="Hello <World>",
        body="Post <b>body</b>",
        meta={"slug": "Hello World!!"},
    )
    adapter = BlogAdapter()

    r1 = adapter.publish(it, p, c, None)
    r2 = adapter.publish(it, p, c, None)  # idempotent: same path, overwrite
    # slug is suffixed with the item id so two items sharing a slug never collide.
    expected = f"https://acme.example/blog/hello-world-{it.id}"
    assert r1.external_url == r2.external_url == expected
    path = tmp_path / "acme" / "site" / "blog" / f"hello-world-{it.id}.html"
    assert path.exists()
    html = path.read_text()
    assert "&lt;World&gt;" in html  # escaped, no XSS injection into the page

    adapter.delete(r1.external_url, p, c, None)
    assert not path.exists()


def test_blog_same_slug_different_items_do_not_collide(session, tmp_path, monkeypatch):
    monkeypatch.setattr("app.workspace.settings.workspace_root", str(tmp_path))
    p = _product(session, domain="acme.example")
    c = _channel(session, p.id)
    a = _item(session, p.id, c.id, body="A", meta={"slug": "dup"})
    b = _item(session, p.id, c.id, body="B", meta={"slug": "dup"})
    adapter = BlogAdapter()

    ra = adapter.publish(a, p, c, None)
    rb = adapter.publish(b, p, c, None)
    assert ra.external_url != rb.external_url  # distinct URLs
    blog = tmp_path / "acme" / "site" / "blog"
    assert (blog / f"dup-{a.id}.html").read_text().count("A") >= 1
    assert (blog / f"dup-{b.id}.html").read_text().count("B") >= 1  # neither overwrote the other


def test_blog_slug_falls_back_to_item_id(session, tmp_path, monkeypatch):
    monkeypatch.setattr("app.workspace.settings.workspace_root", str(tmp_path))
    p = _product(session, domain=None)
    c = _channel(session, p.id)
    it = _item(session, p.id, c.id, meta=None)
    r = BlogAdapter().publish(it, p, c, None)
    assert r.external_url.endswith(f"/blog/post-{it.id}")  # "post" prefix + item id when no slug


# --- reddit adapter (AC1, AC5) -----------------------------------------------------------------


class _FakeSubmissionRow:
    """A prior submission the remote-idempotency scan can match on."""

    def __init__(self, title, subreddit, permalink):
        self.title = title
        self.subreddit = SimpleNamespace(display_name=subreddit)
        self.permalink = permalink


class _FakeSubmission:
    permalink = "/r/test/comments/abc/hi/"


class _FakeSubreddit:
    def __init__(self, record):
        self.record = record

    def submit(self, *, title, selftext, flair_id):
        self.record.update(title=title, selftext=selftext, flair_id=flair_id)
        return _FakeSubmission()


class _FakeReddit:
    def __init__(self, record, existing=None):
        self.record = record
        self._existing = existing or []
        submissions = SimpleNamespace(new=lambda limit=None: list(self._existing))
        self.user = SimpleNamespace(me=lambda: SimpleNamespace(submissions=submissions))

    def subreddit(self, name):
        self.record["subreddit"] = name
        return _FakeSubreddit(self.record)


def test_reddit_adapter_submits_to_configured_subreddit(session, monkeypatch):
    record: dict = {}
    monkeypatch.setattr("app.channels.reddit._build_reddit", lambda creds: _FakeReddit(record))
    p = _product(session)
    c = _channel(
        session,
        p.id,
        ctype=ChannelType.REDDIT,
        profile={"subreddit": "SideProject", "flair_id": "f1"},
    )
    it = _item(session, p.id, c.id, title="Launch", body="value first")
    creds = json.dumps({"client_id": "x", "client_secret": "y", "user_agent": "z"})

    r = RedditAdapter().publish(it, p, c, creds)
    assert r.external_url == "https://www.reddit.com/r/test/comments/abc/hi/"
    assert record["subreddit"] == "SideProject"
    assert record["title"] == "Launch" and record["flair_id"] == "f1"


def test_reddit_idempotent_returns_existing_post_without_reposting(session, monkeypatch):
    # A prior attempt already submitted this title to the subreddit; the remote check must find it
    # and return its permalink instead of double-posting (S4.5 "check remote before re-post").
    record: dict = {}
    existing = [_FakeSubmissionRow("Launch", "SideProject", "/r/SideProject/comments/z/launch/")]
    monkeypatch.setattr(
        "app.channels.reddit._build_reddit", lambda creds: _FakeReddit(record, existing)
    )
    p = _product(session)
    c = _channel(session, p.id, ctype=ChannelType.REDDIT, profile={"subreddit": "SideProject"})
    it = _item(session, p.id, c.id, title="Launch", body="value first")

    r = RedditAdapter().publish(it, p, c, json.dumps({"client_id": "x"}))
    assert r.external_url == "https://www.reddit.com/r/SideProject/comments/z/launch/"
    assert "title" not in record  # submit() was never called


def test_reddit_missing_subreddit_is_permanent_error(session):
    p = _product(session)
    c = _channel(session, p.id, ctype=ChannelType.REDDIT, profile={})
    it = _item(session, p.id, c.id)
    creds = json.dumps({"client_id": "x"})
    with pytest.raises(RuntimeError, match="subreddit"):
        RedditAdapter().publish(it, p, c, creds)


def test_reddit_network_error_is_retryable(session, monkeypatch):
    def boom(creds):
        raise ConnectionError("network down")

    monkeypatch.setattr("app.channels.reddit._build_reddit", boom)
    p = _product(session)
    c = _channel(session, p.id, ctype=ChannelType.REDDIT, profile={"subreddit": "x"})
    it = _item(session, p.id, c.id)
    with pytest.raises(Retryable):
        RedditAdapter().publish(it, p, c, json.dumps({"client_id": "x"}))


def test_get_adapter_rejects_deferred_type():
    with pytest.raises(LookupError):
        get_adapter(ChannelType.INSTAGRAM)
