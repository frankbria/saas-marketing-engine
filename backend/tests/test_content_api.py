"""S4.7: content API — list published items and retract one (real DB, stub adapter, no network)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.api.private import content as content_api
from app.channels.base import Retryable
from app.db import get_session
from app.main import create_app
from app.models import Channel, ContentItem, Product
from app.models.channel import ChannelType
from app.models.content_item import ContentItemStatus

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


class _StubAdapter:
    credential_key = None

    def __init__(self, *, error=None):
        self.error = error
        self.deleted = []

    def delete(self, external_url, product, channel, creds):
        if self.error is not None:
            raise self.error
        self.deleted.append(external_url)


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


def _seed(engine, *, status=ContentItemStatus.PUBLISHED, url="https://acme.example/blog/post-1"):
    with Session(engine) as s:
        p = Product(name="acme", slug="acme", marketing_domain="acme.example")
        s.add(p)
        s.commit()
        s.refresh(p)
        c = Channel(product_id=p.id, type=ChannelType.BLOG, enabled=True, autonomous=True)
        s.add(c)
        s.commit()
        s.refresh(c)
        it = ContentItem(
            product_id=p.id,
            channel_id=c.id,
            content_type="blog",
            status=status,
            body="Body",
            external_url=url,
            published_at=NOW,
        )
        s.add(it)
        s.commit()
        s.refresh(it)
        return p.id, it.id


def test_list_content_returns_published(ctx):
    c, engine = ctx
    pid, iid = _seed(engine)
    resp = c.get(f"/api/private/content/{pid}")
    assert resp.status_code == 200
    body = resp.json()
    assert [i["id"] for i in body] == [iid]
    assert body[0]["status"] == "published"


def test_list_content_missing_product_404(ctx):
    c, _ = ctx
    assert c.get("/api/private/content/999").status_code == 404


def test_retract_published_item(ctx, monkeypatch):
    c, engine = ctx
    pid, iid = _seed(engine)
    stub = _StubAdapter()
    monkeypatch.setattr(content_api, "retract_item", _wrap(stub))

    resp = c.post(f"/api/private/content/{pid}/{iid}/retract")
    assert resp.status_code == 200
    assert resp.json()["status"] == "retracted"
    assert stub.deleted == ["https://acme.example/blog/post-1"]


def test_retract_non_published_409(ctx):
    c, engine = ctx
    pid, iid = _seed(engine, status=ContentItemStatus.SCHEDULED)
    assert c.post(f"/api/private/content/{pid}/{iid}/retract").status_code == 409


def test_retract_missing_external_url_409(ctx):
    c, engine = ctx
    pid, iid = _seed(engine, url=None)
    assert c.post(f"/api/private/content/{pid}/{iid}/retract").status_code == 409


def test_retract_wrong_product_404(ctx):
    c, engine = ctx
    pid, _ = _seed(engine)
    assert c.post(f"/api/private/content/{pid}/999/retract").status_code == 404


def test_retract_transient_failure_503(ctx, monkeypatch):
    c, engine = ctx
    pid, iid = _seed(engine)

    def _boom(session, item, **_):
        raise Retryable("network")

    monkeypatch.setattr(content_api, "retract_item", _boom)
    assert c.post(f"/api/private/content/{pid}/{iid}/retract").status_code == 503


def _wrap(stub):
    """Drive the real retract_item but with the stub adapter injected."""
    from app.modules.crank.retract import retract_item as real

    def _inner(session, item, **_):
        return real(session, item, adapter_for=lambda _t: stub)

    return _inner
