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
    connect_state=None,
):
    c = Channel(
        product_id=product_id,
        type=ctype,
        enabled=enabled,
        autonomous=autonomous,
        paused=paused,
        daily_cap=daily_cap,
        profile_json=json.dumps(profile) if profile else None,
        **({"connect_state": connect_state} if connect_state is not None else {}),
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

        self.creds_seen: list[str | None] = []  # creds handed to publish (S4.8 refresh checks)

    def publish(self, item, product, channel, creds):
        self.calls.append(item.id)
        self.creds_seen.append(creds)
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


def test_publish_pause_halts_then_resume_restores(session):
    # AC #2 as one flow: a due item is halted while the channel is paused, then publishes once
    # resumed — no rescheduling needed, the item just waits at `scheduled`.
    p = _product(session)
    c = _channel(session, p.id)
    it = _scheduled_item(session, p, c)
    stub = StubAdapter()

    c.paused = True
    session.add(c)
    session.commit()
    assert publish_scheduled(session, NOW, adapter_for=lambda t: stub) == []
    session.refresh(it)
    assert it.status == ContentItemStatus.SCHEDULED  # halted while paused, still due

    c.paused = False
    session.add(c)
    session.commit()
    published = publish_scheduled(session, NOW, adapter_for=lambda t: stub)
    assert [i.id for i in published] == [it.id]  # resume restores the schedule
    session.refresh(it)
    assert it.status == ContentItemStatus.PUBLISHED


def test_publish_autonomy_off_after_scheduling_halts_publish(session):
    # pace_content only schedules autonomous channels, but publish must re-check: turning autonomy
    # off after an item is scheduled halts the publish (item stays scheduled).
    p = _product(session)
    c = _channel(session, p.id)
    it = _scheduled_item(session, p, c)
    c.autonomous = False
    session.add(c)
    session.commit()
    stub = StubAdapter()

    assert publish_scheduled(session, NOW, adapter_for=lambda t: stub) == []
    assert stub.calls == []
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
    """A prior submission the remote-idempotency scan matches on (keyed by the body ref marker)."""

    def __init__(self, selftext, subreddit, permalink):
        self.selftext = selftext
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
    it.idempotency_key = "reddit:1"  # set by the pace pass in production
    creds = json.dumps({"client_id": "x", "client_secret": "y", "user_agent": "z"})

    r = RedditAdapter().publish(it, p, c, creds)
    assert r.external_url == "https://www.reddit.com/r/test/comments/abc/hi/"
    assert record["subreddit"] == "SideProject"
    assert record["title"] == "Launch" and record["flair_id"] == "f1"
    assert "sme-ref:reddit:1" in record["selftext"]  # idempotency marker embedded in the post body


def test_reddit_idempotent_returns_existing_post_without_reposting(session, monkeypatch):
    # A prior attempt already submitted this item (its ref marker is on a remote post); the check
    # must find it by idempotency_key and return its permalink, never double-posting. Two items
    # sharing a title but different keys must NOT collide — hence the marker, not the title.
    record: dict = {}
    existing = [
        _FakeSubmissionRow(
            "value first\n\n^(sme-ref:reddit:7)", "SideProject", "/r/SideProject/comments/z/launch/"
        )
    ]
    monkeypatch.setattr(
        "app.channels.reddit._build_reddit", lambda creds: _FakeReddit(record, existing)
    )
    p = _product(session)
    c = _channel(session, p.id, ctype=ChannelType.REDDIT, profile={"subreddit": "SideProject"})
    it = _item(session, p.id, c.id, title="Launch", body="value first")
    it.idempotency_key = "reddit:7"

    r = RedditAdapter().publish(it, p, c, json.dumps({"client_id": "x"}))
    assert r.external_url == "https://www.reddit.com/r/SideProject/comments/z/launch/"
    assert "title" not in record  # submit() was never called


def test_reddit_same_title_different_key_does_not_dedup(session, monkeypatch):
    # A remote post exists for a DIFFERENT item (key reddit:7) that happens to share the title.
    # The new item (key reddit:8) must still post — title collisions must not suppress it.
    record: dict = {}
    existing = [
        _FakeSubmissionRow("older\n\n^(sme-ref:reddit:7)", "SideProject", "/r/x/comments/old/")
    ]
    monkeypatch.setattr(
        "app.channels.reddit._build_reddit", lambda creds: _FakeReddit(record, existing)
    )
    p = _product(session)
    c = _channel(session, p.id, ctype=ChannelType.REDDIT, profile={"subreddit": "SideProject"})
    it = _item(session, p.id, c.id, title="Launch", body="newer")
    it.idempotency_key = "reddit:8"

    RedditAdapter().publish(it, p, c, json.dumps({"client_id": "x"}))
    assert record.get("selftext", "").startswith("newer")  # actually submitted, not deduped


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
    it.idempotency_key = "reddit:1"
    with pytest.raises(Retryable):
        RedditAdapter().publish(it, p, c, json.dumps({"client_id": "x"}))


def test_reddit_permanent_error_propagates_not_retryable(session, monkeypatch):
    # A permanent Reddit API/validation error (not a network blip) must surface as-is so the
    # publish pass records `publish_failed` instead of retrying a doomed post forever.
    def boom(creds):
        raise ValueError("SUBREDDIT_NOTALLOWED: banned")

    monkeypatch.setattr("app.channels.reddit._build_reddit", boom)
    p = _product(session)
    c = _channel(session, p.id, ctype=ChannelType.REDDIT, profile={"subreddit": "x"})
    it = _item(session, p.id, c.id)
    it.idempotency_key = "reddit:1"
    with pytest.raises(ValueError):  # not wrapped in Retryable
        RedditAdapter().publish(it, p, c, json.dumps({"client_id": "x"}))


def test_reddit_missing_idempotency_key_fails_closed(session):
    # No idempotency_key => the remote guard can't work; refuse the non-idempotent submit.
    p = _product(session)
    c = _channel(session, p.id, ctype=ChannelType.REDDIT, profile={"subreddit": "x"})
    it = _item(session, p.id, c.id)  # idempotency_key defaults to None
    with pytest.raises(RuntimeError, match="idempotency_key"):
        RedditAdapter().publish(it, p, c, json.dumps({"client_id": "x"}))


def test_reddit_ratelimit_api_exception_is_retryable():
    # A RATELIMIT RedditAPIException is transient (Reddit's wait exceeded ratelimit_seconds); other
    # RedditAPIException items (validation/auth) are permanent. praw is a real dependency here.
    from praw.exceptions import RedditAPIException

    from app.channels.reddit import _is_transient

    assert _is_transient(RedditAPIException([["RATELIMIT", "doing that too much", None]])) is True
    assert _is_transient(RedditAPIException([["NO_TEXT", "missing text", "title"]])) is False


def test_get_adapter_rejects_deferred_type():
    with pytest.raises(LookupError):
        get_adapter(ChannelType.INSTAGRAM)


# --- S4.8: OAuth refresh handling (fail-safe) --------------------------------------------------

from app.models import ConnectState  # noqa: E402
from app.secrets import vault  # noqa: E402


@pytest.fixture(autouse=True)
def _vault_key(monkeypatch):
    """Seed the Fernet vault key so credential-using tests work in CI (SME_VAULT_KEY unset there),
    mirroring test_vault.py / test_channels_api.py. Autouse: harmless for the non-vault tests."""
    monkeypatch.setattr(vault.settings, "vault_key", vault.generate_key())


def _oauth_channel(session, product_id, *, expires_at, token="tok-old"):
    """A CONNECTED reddit channel with a stored oauth token expiring at `expires_at`."""
    c = _channel(
        session,
        product_id,
        ctype=ChannelType.REDDIT,
        profile={"subreddit": "x"},
        connect_state=ConnectState.CONNECTED,
    )
    vault.put_credential(
        session, product_id, "reddit_oauth", token, channel_id=c.id, expires_at=expires_at
    )
    return c


def test_publish_halts_on_failed_connect_state(session):
    # AC2: a channel marked `failed` (dead token) halts its publishes; item stays scheduled.
    p = _product(session)
    c = _channel(session, p.id, connect_state=ConnectState.FAILED)
    it = _scheduled_item(session, p, c)
    stub = StubAdapter()

    assert publish_scheduled(session, NOW, adapter_for=lambda t: stub) == []
    assert stub.calls == []  # never published for a failed channel
    session.refresh(it)
    assert it.status == ContentItemStatus.SCHEDULED


def test_publish_refreshes_token_near_expiry_then_publishes(session):
    # AC1: a token within the refresh buffer is proactively refreshed before publishing, and the
    # publish then uses the fresh credential.
    p = _product(session)
    c = _oauth_channel(session, p.id, expires_at=NOW + timedelta(minutes=1), token="tok-old")
    it = _scheduled_item(session, p, c)
    stub = StubAdapter(credential_key="reddit_oauth")

    def fake_refresh(session, product, channel, now):
        vault.put_credential(
            session,
            product.id,
            "reddit_oauth",
            "tok-new",
            channel_id=channel.id,
            expires_at=now + timedelta(days=1),
        )

    published = publish_scheduled(session, NOW, adapter_for=lambda t: stub, refresh=fake_refresh)
    assert [i.id for i in published] == [it.id]
    assert stub.creds_seen == ["tok-new"]  # published with the refreshed token


def test_publish_does_not_refresh_when_token_fresh(session):
    # A token well beyond the buffer is left alone (no needless refresh).
    p = _product(session)
    c = _oauth_channel(session, p.id, expires_at=NOW + timedelta(days=30), token="tok-old")
    _scheduled_item(session, p, c)
    stub = StubAdapter(credential_key="reddit_oauth")

    def boom(*a, **k):
        raise AssertionError("refresh must not run for a fresh token")

    published = publish_scheduled(session, NOW, adapter_for=lambda t: stub, refresh=boom)
    assert len(published) == 1
    assert stub.creds_seen == ["tok-old"]


def test_publish_does_not_refresh_when_no_expiry(session):
    # No stored expiry => nothing to proactively refresh; publish proceeds with the current token.
    p = _product(session)
    c = _oauth_channel(session, p.id, expires_at=None, token="tok-old")
    _scheduled_item(session, p, c)
    stub = StubAdapter(credential_key="reddit_oauth")

    def boom(*a, **k):
        raise AssertionError("refresh must not run without a known expiry")

    assert len(publish_scheduled(session, NOW, adapter_for=lambda t: stub, refresh=boom)) == 1


def test_publish_refresh_failure_marks_failed_halts_and_alerts(session, caplog):
    # AC2: a failed refresh sets the channel `failed`, halts the publish (item stays scheduled),
    # and fires an alert.
    p = _product(session)
    c = _oauth_channel(session, p.id, expires_at=NOW + timedelta(seconds=30))
    it = _scheduled_item(session, p, c)
    stub = StubAdapter(credential_key="reddit_oauth")

    def dead_refresh(*a, **k):
        raise RuntimeError("refresh token revoked")

    with caplog.at_level("WARNING"):
        published = publish_scheduled(
            session, NOW, adapter_for=lambda t: stub, refresh=dead_refresh
        )

    assert published == []
    assert stub.calls == []  # never attempted a publish on the dead channel
    session.refresh(c)
    assert c.connect_state == ConnectState.FAILED
    session.refresh(it)
    assert it.status == ContentItemStatus.SCHEDULED  # halted, not failed — resumes once reconnected
    assert any("oauth_refresh_failed" in r.getMessage() for r in caplog.records)


def test_pace_skips_failed_channel(session):
    # A failed channel should not accrue newly-scheduled items either.
    p = _product(session)
    c = _channel(session, p.id, connect_state=ConnectState.FAILED)
    _item(session, p.id, c.id)
    assert pace_content(session, NOW) == []
    assert all(
        r.status == ContentItemStatus.CRITIC_PASSED for r in session.exec(select(ContentItem)).all()
    )


def test_publish_proceeds_when_no_refresh_handler_registered(session):
    # A bare (owned) token near expiry whose provider has no registered token endpoint can't be
    # proactively refreshed — that's a config gap, not a refresh failure. We proceed (the token may
    # still be valid) and rely on the reactive AuthFailure fence if it's actually dead, rather than
    # needlessly fencing a live channel. (v1 registers no endpoints; TOKEN_ENDPOINTS is empty.)
    p = _product(session)
    c = _oauth_channel(session, p.id, expires_at=NOW + timedelta(minutes=1), token="tok-old")
    it = _scheduled_item(session, p, c)
    stub = StubAdapter(credential_key="reddit_oauth")

    published = publish_scheduled(session, NOW, adapter_for=lambda t: stub)
    assert [i.id for i in published] == [it.id]
    assert stub.creds_seen == ["tok-old"]  # published with the existing token
    session.refresh(c)
    assert c.connect_state == ConnectState.CONNECTED  # not fenced — config gap ≠ dead token


def test_publish_skips_refresh_for_self_managed_credential(session):
    # A structured reddit_oauth blob (PRAW kwargs) is self-refreshed by PRAW under the hood — even
    # near expiry we must NOT run our bare-token refresher (which would corrupt the shape) and must
    # NOT fail the channel. Publish proceeds, handing the untouched JSON blob to the adapter.
    p = _product(session)
    c = _channel(session, p.id, ctype=ChannelType.REDDIT, connect_state=ConnectState.CONNECTED)
    blob = json.dumps({"client_id": "x", "refresh_token": "z"})
    vault.put_credential(
        session, p.id, "reddit_oauth", blob, channel_id=c.id, expires_at=NOW + timedelta(minutes=1)
    )
    it = _scheduled_item(session, p, c)
    stub = StubAdapter(credential_key="reddit_oauth")

    def boom(*a, **k):
        raise AssertionError("bare-token refresher must not run for a self-managed credential")

    published = publish_scheduled(session, NOW, adapter_for=lambda t: stub, refresh=boom)
    assert [i.id for i in published] == [it.id]
    assert stub.creds_seen == [blob]  # adapter got the untouched JSON blob
    session.refresh(c)
    assert c.connect_state == ConnectState.CONNECTED  # healthy channel not fenced off


def test_publish_adapter_lookup_failure_is_isolated(session):
    # §8.3 isolation: an adapter-lookup failure on one item marks just that item publish_failed and
    # must not abort the pass — a sibling item on a working channel still publishes.
    p = _product(session)
    bad = _channel(session, p.id, ctype=ChannelType.BLOG)
    good = _channel(session, p.id, ctype=ChannelType.REDDIT, profile={"subreddit": "x"})
    bad_it = _scheduled_item(session, p, bad)
    good_it = _scheduled_item(session, p, good)
    stub = StubAdapter()

    def adapter_for(ctype):
        if ctype == ChannelType.BLOG:
            raise LookupError("no adapter")
        return stub

    published = publish_scheduled(session, NOW, adapter_for=adapter_for)
    assert [i.id for i in published] == [good_it.id]
    session.refresh(bad_it)
    assert bad_it.status == ContentItemStatus.PUBLISH_FAILED


def test_reddit_auth_error_classified_as_auth_failure():
    # A 401 or OAuth error means the (self-managed) credential is dead — classified as an auth
    # failure so the publish pass fences the whole channel. A 403 (subreddit permission/policy
    # denial) is a per-post problem, NOT a dead token, so it stays a per-item publish_failure.
    from prawcore.exceptions import ResponseException

    from app.channels.reddit import _is_auth_failure

    assert _is_auth_failure(ResponseException(SimpleNamespace(status_code=401))) is True
    assert _is_auth_failure(ResponseException(SimpleNamespace(status_code=403))) is False
    assert _is_auth_failure(ResponseException(SimpleNamespace(status_code=400))) is False
    assert _is_auth_failure(ValueError("bad title")) is False


def test_publish_auth_failure_fences_channel(session, caplog):
    # AC2 for the real self-managed provider: an AuthFailure raised during publish fences the whole
    # channel (connect_state=FAILED + alert) and leaves the item scheduled (resumes on reconnect),
    # rather than a per-item publish_failed that leaves the dead channel CONNECTED.
    from app.channels.base import AuthFailure

    p = _product(session)
    c = _channel(session, p.id, ctype=ChannelType.REDDIT, connect_state=ConnectState.CONNECTED)
    it = _scheduled_item(session, p, c)
    stub = StubAdapter(credential_key="reddit_oauth", error=AuthFailure("token revoked"))

    with caplog.at_level("WARNING"):
        assert publish_scheduled(session, NOW, adapter_for=lambda t: stub) == []
    session.refresh(c)
    assert c.connect_state == ConnectState.FAILED
    session.refresh(it)
    assert it.status == ContentItemStatus.SCHEDULED  # not publish_failed — resumes on reconnect
    assert any("oauth_refresh_failed" in r.getMessage() for r in caplog.records)


def test_refresh_channel_token_grant_updates_bare_token(session, monkeypatch):
    # The owned-token path (the /connect bare-token shape) runs a real OAuth2 refresh grant: it
    # reads the refresh token + client creds from the vault, calls the provider token endpoint
    # (injected here — network boundary), and writes the new bare access token + fresh expiry back.
    from app.modules.crank import oauth_refresh
    from app.secrets.vault import get_credential_expiry

    p = _product(session)
    c = _channel(session, p.id, ctype=ChannelType.REDDIT, connect_state=ConnectState.CONNECTED)
    vault.put_credential(session, p.id, "reddit_oauth", "old", channel_id=c.id, expires_at=NOW)
    vault.put_credential(session, p.id, "reddit_oauth_refresh", "rtok", channel_id=c.id)
    vault.put_credential(session, p.id, "reddit_client_id", "cid", channel_id=c.id)
    vault.put_credential(session, p.id, "reddit_client_secret", "csec", channel_id=c.id)
    monkeypatch.setitem(oauth_refresh.TOKEN_ENDPOINTS, ChannelType.REDDIT, "https://ex/token")

    captured = {}

    def fake_post(endpoint, refresh_token, client_id, client_secret):
        captured.update(endpoint=endpoint, refresh=refresh_token, cid=client_id, csec=client_secret)
        return {"access_token": "new", "expires_in": 3600}

    monkeypatch.setattr(oauth_refresh, "_post_token_refresh", fake_post)

    oauth_refresh.refresh_channel_token(session, p, c, NOW)

    assert captured == {
        "endpoint": "https://ex/token",
        "refresh": "rtok",
        "cid": "cid",
        "csec": "csec",
    }
    assert vault.get_credential(session, p.id, "reddit_oauth", channel_id=c.id) == "new"
    stored_expiry = get_credential_expiry(session, p.id, "reddit_oauth", channel_id=c.id)
    assert _utc(stored_expiry) == NOW + timedelta(seconds=3600)


def test_refresh_channel_token_persists_rotated_refresh_token(session, monkeypatch):
    # Refresh-token rotation: when the grant returns a new refresh_token, it must be persisted (the
    # provider revokes the old one), or the next refresh would present a dead token.
    from app.modules.crank import oauth_refresh

    p = _product(session)
    c = _channel(session, p.id, ctype=ChannelType.REDDIT, connect_state=ConnectState.CONNECTED)
    vault.put_credential(session, p.id, "reddit_oauth", "old", channel_id=c.id, expires_at=NOW)
    vault.put_credential(session, p.id, "reddit_oauth_refresh", "rtok-old", channel_id=c.id)
    vault.put_credential(session, p.id, "reddit_client_id", "cid", channel_id=c.id)
    vault.put_credential(session, p.id, "reddit_client_secret", "csec", channel_id=c.id)
    monkeypatch.setitem(oauth_refresh.TOKEN_ENDPOINTS, ChannelType.REDDIT, "https://ex/token")
    monkeypatch.setattr(
        oauth_refresh,
        "_post_token_refresh",
        lambda *a: {"access_token": "new", "refresh_token": "rtok-new", "expires_in": 3600},
    )

    oauth_refresh.refresh_channel_token(session, p, c, NOW)

    assert (
        vault.get_credential(session, p.id, "reddit_oauth_refresh", channel_id=c.id) == "rtok-new"
    )


def test_registered_provider_grant_failure_fences_channel(session, monkeypatch, caplog):
    # S4.8.2 AC: for a *registered* owned-token provider (endpoint in TOKEN_ENDPOINTS), a real grant
    # failure at the network seam must fire the S4.8 fail-safe end-to-end — the channel is fenced
    # (connect_state=FAILED) and `oauth_refresh_failed` alerted — not just when refresh is stubbed.
    from app.modules.crank import oauth_refresh

    p = _product(session)
    c = _oauth_channel(session, p.id, expires_at=NOW + timedelta(seconds=30))
    it = _scheduled_item(session, p, c)
    vault.put_credential(session, p.id, "reddit_oauth_refresh", "rtok", channel_id=c.id)
    vault.put_credential(session, p.id, "reddit_client_id", "cid", channel_id=c.id)
    vault.put_credential(session, p.id, "reddit_client_secret", "csec", channel_id=c.id)
    monkeypatch.setitem(oauth_refresh.TOKEN_ENDPOINTS, ChannelType.REDDIT, "https://ex/token")

    def dead_grant(*a):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(oauth_refresh, "_post_token_refresh", dead_grant)

    stub = StubAdapter(credential_key="reddit_oauth")
    with caplog.at_level("WARNING"):
        # default refresh=refresh_channel_token — the real grant path, not an injected stub
        published = publish_scheduled(session, NOW, adapter_for=lambda t: stub)

    assert published == []
    assert stub.calls == []
    session.refresh(c)
    assert c.connect_state == ConnectState.FAILED
    session.refresh(it)
    assert it.status == ContentItemStatus.SCHEDULED  # halted, resumes on reconnect
    assert any("oauth_refresh_failed" in r.getMessage() for r in caplog.records)
    # the fail-safe alert must not leak the seeded refresh token / client secret into the log
    assert not any("rtok" in r.getMessage() or "csec" in r.getMessage() for r in caplog.records)
