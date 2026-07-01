"""S4.7: retract — `retract_item` deletes the remote post and flips the item to `retracted`.

Real DB, stub adapter (no network), same seam as test_publish. Also covers the RedditAdapter.delete
transient-error path, which S4.5 left untested."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.channels.base import Retryable
from app.channels.reddit import RedditAdapter
from app.models import Channel, ContentItem, Product
from app.models.channel import ChannelType
from app.models.content_item import ContentItemStatus
from app.modules.crank.retract import retract_item

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


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


def _published(session):
    p = Product(name="acme", slug="acme", marketing_domain="acme.example")
    session.add(p)
    session.commit()
    session.refresh(p)
    c = Channel(product_id=p.id, type=ChannelType.BLOG, enabled=True, autonomous=True)
    session.add(c)
    session.commit()
    session.refresh(c)
    it = ContentItem(
        product_id=p.id,
        channel_id=c.id,
        content_type="blog",
        status=ContentItemStatus.PUBLISHED,
        body="Body",
        external_url="https://acme.example/blog/post-1",
        published_at=NOW,
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


class _StubAdapter:
    credential_key = None

    def __init__(self, *, error=None):
        self.error = error
        self.deleted: list[str] = []

    def delete(self, external_url, product, channel, creds):
        if self.error is not None:
            raise self.error
        self.deleted.append(external_url)


def test_retract_deletes_remote_and_marks_retracted(session):
    it = _published(session)
    stub = _StubAdapter()

    out = retract_item(session, it, adapter_for=lambda _t: stub)

    assert out.status == ContentItemStatus.RETRACTED
    assert stub.deleted == ["https://acme.example/blog/post-1"]  # deleted by external_url
    assert session.get(ContentItem, it.id).status == ContentItemStatus.RETRACTED


def test_retract_transient_failure_propagates_and_stays_published(session):
    it = _published(session)
    stub = _StubAdapter(error=Retryable("network"))

    with pytest.raises(Retryable):
        retract_item(session, it, adapter_for=lambda _t: stub)

    session.rollback()
    assert session.get(ContentItem, it.id).status == ContentItemStatus.PUBLISHED


def test_retract_missing_external_url_fails_closed(session):
    # A published row without a delete handle must not be marked retracted (the live post would
    # stay up while the dashboard claims it's gone).
    it = _published(session)
    it.external_url = None
    session.add(it)
    session.commit()
    stub = _StubAdapter()

    with pytest.raises(ValueError):
        retract_item(session, it, adapter_for=lambda _t: stub)
    assert stub.deleted == []
    assert session.get(ContentItem, it.id).status == ContentItemStatus.PUBLISHED


def test_reddit_delete_network_error_is_retryable(session, monkeypatch):
    # S4.5 left the reddit delete path untested; retract exercises it. A connectivity error must
    # raise Retryable so the operator retries rather than the post being falsely marked retracted.
    class _Boom:
        def submission(self, url):
            raise ConnectionError("down")

    monkeypatch.setattr("app.channels.reddit._build_reddit", lambda creds: _Boom())
    with pytest.raises(Retryable):
        RedditAdapter().delete("https://reddit.com/r/x/comments/1", None, None, '{"a": 1}')
