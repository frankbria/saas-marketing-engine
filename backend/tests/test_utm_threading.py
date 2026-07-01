"""S6.1: UTM threading — build/thread/parse helpers, plus the publish-time hookup.

Real SQLite + a stub adapter (no network), matching the `tests/test_publish.py` house style.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Channel, ChannelType, ContentItem, ContentItemStatus, Product
from app.modules.crank.publish import publish_scheduled
from app.modules.metrics.utm import parse_utm_content, thread_utm_links, utm_params

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


def _product(session, *, slug="acme", domain="acme.example"):
    p = Product(name=slug, slug=slug, marketing_domain=domain)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _channel(session, product_id, *, ctype=ChannelType.REDDIT):
    c = Channel(product_id=product_id, type=ctype, enabled=True, autonomous=True)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _item(session, product_id, channel_id, *, content_type="social", body="Body", **kw):
    it = ContentItem(
        product_id=product_id, channel_id=channel_id, content_type=content_type, body=body, **kw
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def _query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


# --- thread_utm_links ----------------------------------------------------------------------


def test_threads_bare_domain_link(session):
    p = _product(session)
    c = _channel(session, p.id)
    it = _item(session, p.id, c.id, body="Check it out: https://acme.example/landing")

    out = thread_utm_links(it.body, p, c, it)

    assert out.startswith("Check it out: https://acme.example/landing?")
    q = _query(out.split(": ", 1)[1])
    assert q == utm_params(p, c, it)


def test_threads_link_with_path_and_existing_query(session):
    p = _product(session)
    c = _channel(session, p.id)
    it = _item(
        session, p.id, c.id, body="https://acme.example/promo/launch?ref=abc&utm_source=other"
    )

    out = thread_utm_links(it.body, p, c, it)

    q = _query(out)
    assert q["ref"] == "abc"  # existing non-UTM param preserved
    assert q["utm_source"] == "reddit"  # ours wins on key collision
    assert urlsplit(out).path == "/promo/launch"
    assert q == {"ref": "abc", **utm_params(p, c, it)}


def test_threads_multiple_links_independently(session):
    p = _product(session)
    c = _channel(session, p.id)
    it = _item(
        session,
        p.id,
        c.id,
        body="First https://acme.example/a then https://acme.example/b again.",
    )

    out = thread_utm_links(it.body, p, c, it)

    urls = [tok for tok in out.split() if tok.startswith("https://acme.example")]
    assert len(urls) == 2
    for url in urls:
        assert _query(url) == utm_params(p, c, it)
    assert urlsplit(urls[0]).path == "/a"
    assert urlsplit(urls[1]).path == "/b"


def test_threads_link_followed_by_sentence_period(session):
    p = _product(session)
    c = _channel(session, p.id)
    it = _item(session, p.id, c.id, body="Read https://acme.example/landing.")

    out = thread_utm_links(it.body, p, c, it)

    url = next(t for t in out.split() if "acme.example/landing" in t)
    assert urlsplit(url).path == "/landing"
    assert out.endswith(".")


def test_threads_link_inside_markdown_link(session):
    p = _product(session)
    c = _channel(session, p.id)
    it = _item(session, p.id, c.id, body="Learn [more](https://acme.example/landing)")

    out = thread_utm_links(it.body, p, c, it)

    assert out.startswith("Learn [more](https://acme.example/landing?")
    assert out.endswith(")")
    assert _query(out.split("(")[1].rstrip(")")) == utm_params(p, c, it)


def test_threads_link_followed_by_comma(session):
    p = _product(session)
    c = _channel(session, p.id)
    it = _item(session, p.id, c.id, body="Visit https://acme.example/landing, today.")

    out = thread_utm_links(it.body, p, c, it)

    url = next(t for t in out.split() if "acme.example/landing" in t)
    assert urlsplit(url).path == "/landing"
    assert ", today." in out


def test_no_marketing_domain_returns_body_unchanged(session):
    p = _product(session, domain=None)
    c = _channel(session, p.id)
    it = _item(session, p.id, c.id, body="See https://acme.example/x")

    assert thread_utm_links(it.body, p, c, it) == it.body


def test_no_matching_link_returns_body_unchanged(session):
    p = _product(session)
    c = _channel(session, p.id)
    it = _item(session, p.id, c.id, body="See https://other.example/x for details")

    assert thread_utm_links(it.body, p, c, it) == it.body


# --- parse_utm_content -----------------------------------------------------------------------


def test_parse_utm_content_round_trip(session):
    p = _product(session)
    c = _channel(session, p.id)
    it = _item(session, p.id, c.id)

    assert parse_utm_content(utm_params(p, c, it)["utm_content"]) == it.id


@pytest.mark.parametrize("value", [None, "", "sme-", "sme-abc", "other-1"])
def test_parse_utm_content_rejects_malformed(value):
    assert parse_utm_content(value) is None


# --- publish_scheduled hookup ------------------------------------------------------------------


class _RecordingAdapter:
    credential_key = None

    def __init__(self):
        self.bodies: list[str] = []

    def publish(self, item, product, channel, creds):
        from app.channels.base import PublishResult

        self.bodies.append(item.body)
        return PublishResult(external_url="https://acme.example/blog/x")

    def delete(self, external_url, product, channel, creds):  # pragma: no cover - unused
        pass


def test_publish_scheduled_threads_utm_into_published_body(session):
    p = _product(session)
    c = _channel(session, p.id)
    it = _item(
        session,
        p.id,
        c.id,
        body="Read more at https://acme.example/post",
        status=ContentItemStatus.SCHEDULED,
        scheduled_for=NOW,
        idempotency_key="reddit:1",
    )
    adapter = _RecordingAdapter()

    published = publish_scheduled(session, NOW, adapter_for=lambda _t: adapter)

    assert [i.id for i in published] == [it.id]
    expected_query = utm_params(p, c, it)
    assert adapter.bodies == [it.body]  # adapter received the already-threaded body
    assert _query(adapter.bodies[0]) == expected_query

    stored = session.exec(select(ContentItem).where(ContentItem.id == it.id)).one()
    assert _query(stored.body) == expected_query  # stored body matches the published artifact
