"""S6.2.1: the platform side of reach ingestion (issue #79).

`test_reach_poll.py` covers the pass's arithmetic with a fake adapter; this file covers the other
half — that `fetch_reach` actually reads the right field out of each platform's real response
shape, and that a missing/withdrawn counter comes back as `None` rather than a zero. That
distinction is load-bearing: `None` means "no number", a zero means "nobody saw it", and only the
second one should be able to raise a shadowban alert.

Reddit goes through the `_build_reddit` seam, YouTube through `httpx.MockTransport` — the same
house pattern the publish adapter tests use. No network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.channels.base import Retryable
from app.channels.blog import BlogAdapter
from app.channels.podcast import PodcastAdapter
from app.channels.reddit import RedditAdapter
from app.channels.youtube import YouTubeAdapter
from app.models import Channel, ChannelType, ContentItem, Product

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
REDDIT_CREDS = '{"client_id": "x", "client_secret": "y", "user_agent": "z"}'


@pytest.fixture
def session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _fixture(session, ctype, url):
    product = Product(name="Acme", slug="acme")
    session.add(product)
    session.commit()
    session.refresh(product)
    channel = Channel(product_id=product.id, type=ctype, enabled=True, autonomous=True)
    session.add(channel)
    session.commit()
    session.refresh(channel)
    item = ContentItem(
        product_id=product.id,
        channel_id=channel.id,
        content_type="social",
        body="body",
        external_url=url,
        created_at=NOW,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return product, channel, item


# --- reddit ------------------------------------------------------------------


class _FakeReddit:
    def __init__(self, score=None, raises=None):
        self._score = score
        self._raises = raises
        self.asked: list[str] = []

    def submission(self, url):
        self.asked.append(url)
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(score=self._score)


def test_reddit_reads_the_submission_score(session, monkeypatch):
    product, channel, item = _fixture(session, ChannelType.REDDIT, "https://reddit.test/a")
    fake = _FakeReddit(score=42)
    monkeypatch.setattr("app.channels.reddit._build_reddit", lambda creds: fake)

    assert RedditAdapter().fetch_reach(item, product, channel, REDDIT_CREDS) == 42
    assert fake.asked == ["https://reddit.test/a"]


def test_reddit_returns_none_without_an_external_url(session, monkeypatch):
    """An item that never published (or published without recording a URL) has nothing to poll,
    and must not be reported as zero reach — it was never on the platform to be seen."""
    product, channel, item = _fixture(session, ChannelType.REDDIT, None)

    def _never_called(creds):
        raise AssertionError("no client should be built for an item with no URL")

    monkeypatch.setattr("app.channels.reddit._build_reddit", _never_called)

    assert RedditAdapter().fetch_reach(item, product, channel, REDDIT_CREDS) is None


def test_reddit_transient_error_is_retryable_not_a_zero(session, monkeypatch):
    """A network blip must not read as "nobody saw it" — that would fire a false shadowban alert
    on a healthy channel every time Reddit hiccupped."""
    product, channel, item = _fixture(session, ChannelType.REDDIT, "https://reddit.test/a")
    monkeypatch.setattr(
        "app.channels.reddit._build_reddit",
        lambda creds: _FakeReddit(raises=ConnectionError("network down")),
    )

    with pytest.raises(Retryable):
        RedditAdapter().fetch_reach(item, product, channel, REDDIT_CREDS)


def test_reddit_deleted_submission_returns_none(session, monkeypatch):
    """A removed post has no counter and never will again. `None` drops it quietly instead of
    raising the same permanent error on every tick forever."""
    from prawcore.exceptions import NotFound

    product, channel, item = _fixture(session, ChannelType.REDDIT, "https://reddit.test/a")
    gone = NotFound(httpx.Response(404, request=httpx.Request("GET", "https://reddit.test/a")))
    monkeypatch.setattr("app.channels.reddit._build_reddit", lambda creds: _FakeReddit(raises=gone))

    assert RedditAdapter().fetch_reach(item, product, channel, REDDIT_CREDS) is None


# --- youtube -----------------------------------------------------------------


def _youtube_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.channels.youtube._build_youtube",
        lambda creds: httpx.Client(transport=transport, headers={"Authorization": "Bearer t"}),
    )


def _stats_response(view_count):
    return httpx.Response(200, json={"items": [{"statistics": {"viewCount": view_count}}]})


def test_youtube_reads_viewcount_from_statistics(session, monkeypatch):
    product, channel, item = _fixture(
        session, ChannelType.YOUTUBE, "https://www.youtube.com/watch?v=ABC123"
    )
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        # viewCount is a *string* in the Data API's JSON, not a number.
        return _stats_response("1234")

    _youtube_client(monkeypatch, handler)

    assert YouTubeAdapter().fetch_reach(item, product, channel, "token") == 1234
    assert "part=statistics" in seen[0]
    assert "id=ABC123" in seen[0]


def test_youtube_missing_video_returns_none(session, monkeypatch):
    """A 200 with an empty `items` is how the Data API reports an id it won't serve (deleted, or
    made private after upload). No counter — not a zero."""
    product, channel, item = _fixture(
        session, ChannelType.YOUTUBE, "https://www.youtube.com/watch?v=ABC123"
    )
    _youtube_client(monkeypatch, lambda request: httpx.Response(200, json={"items": []}))

    assert YouTubeAdapter().fetch_reach(item, product, channel, "token") is None


def test_youtube_404_returns_none(session, monkeypatch):
    product, channel, item = _fixture(
        session, ChannelType.YOUTUBE, "https://www.youtube.com/watch?v=ABC123"
    )
    _youtube_client(monkeypatch, lambda request: httpx.Response(404, json={"error": {}}))

    assert YouTubeAdapter().fetch_reach(item, product, channel, "token") is None


def test_youtube_non_numeric_viewcount_returns_none(session, monkeypatch):
    """Response-shape drift skips one item; it must not crash the tick for every other channel."""
    product, channel, item = _fixture(
        session, ChannelType.YOUTUBE, "https://www.youtube.com/watch?v=ABC123"
    )
    _youtube_client(monkeypatch, lambda request: _stats_response("not-a-number"))

    assert YouTubeAdapter().fetch_reach(item, product, channel, "token") is None


def test_youtube_server_error_is_retryable_not_a_zero(session, monkeypatch):
    product, channel, item = _fixture(
        session, ChannelType.YOUTUBE, "https://www.youtube.com/watch?v=ABC123"
    )
    _youtube_client(monkeypatch, lambda request: httpx.Response(503, json={"error": {}}))

    with pytest.raises(Retryable):
        YouTubeAdapter().fetch_reach(item, product, channel, "token")


# --- owned channels ----------------------------------------------------------


@pytest.mark.parametrize(
    ("adapter", "ctype"),
    [(BlogAdapter(), ChannelType.BLOG), (PodcastAdapter(), ChannelType.PODCAST)],
    ids=["blog", "podcast"],
)
def test_owned_adapters_declare_no_platform_reach(session, adapter, ctype):
    """The declaration the heartbeat reads to exclude these channels from the shadowban alert.
    Asserting the flag (not just the None) is the point: the flag is what stops them being polled
    at all, and what keeps "unmeasured" from being reported as "unseen"."""
    product, channel, item = _fixture(session, ctype, "https://acme.test/blog/a")

    assert adapter.has_platform_reach is False
    assert adapter.fetch_reach(item, product, channel, None) is None


def test_youtube_non_json_body_returns_none(session, monkeypatch):
    """A 200 carrying HTML (proxy / captive portal) is "no number", not a crash and not a zero."""
    product, channel, item = _fixture(
        session, ChannelType.YOUTUBE, "https://www.youtube.com/watch?v=ABC123"
    )
    _youtube_client(monkeypatch, lambda request: httpx.Response(200, text="<html>nope</html>"))

    assert YouTubeAdapter().fetch_reach(item, product, channel, "token") is None
