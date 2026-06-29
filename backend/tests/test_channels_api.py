"""S2.6: channels API — setup trigger gates, list endpoints, OAuth connect (real vault), toggles."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session
from app.main import create_app
from app.models import (
    Channel,
    ChannelType,
    ConnectState,
    LifecycleState,
    Product,
    SetupChecklistItem,
)
from app.secrets import vault


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(engine)
    # Real vault, real key — no mocking (house rule). connect writes encrypted ciphertext.
    monkeypatch.setattr(vault.settings, "vault_key", vault.generate_key())

    def _session_override():
        with Session(engine) as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _session_override
    with TestClient(app) as c:
        yield c, engine


def _seed_product(engine, *, state=LifecycleState.SETUP_READY, brand='{"name":"x"}'):
    with Session(engine) as s:
        p = Product(name="Auto Author", slug="auto-author", brand_json=brand, lifecycle_state=state)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _seed_channel(engine, product_id, ctype=ChannelType.REDDIT):
    with Session(engine) as s:
        chan = Channel(product_id=product_id, type=ctype)
        s.add(chan)
        s.commit()
        s.refresh(chan)
        return chan.id


# ---- trigger gates ----------------------------------------------------------------------


def test_trigger_setup_202(ctx):
    c, engine = ctx
    pid = _seed_product(engine)
    resp = c.post(f"/api/private/channels/{pid}/setup")
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"


def test_trigger_missing_product_404(ctx):
    c, _ = ctx
    assert c.post("/api/private/channels/999/setup").status_code == 404


def test_trigger_wrong_state_409(ctx):
    c, engine = ctx
    pid = _seed_product(engine, state=LifecycleState.STRATEGY)
    assert c.post(f"/api/private/channels/{pid}/setup").status_code == 409


def test_trigger_no_brand_400(ctx):
    c, engine = ctx
    pid = _seed_product(engine, brand=None)
    assert c.post(f"/api/private/channels/{pid}/setup").status_code == 400


# ---- list endpoints ---------------------------------------------------------------------


def test_list_channels_and_checklist(ctx):
    c, engine = ctx
    pid = _seed_product(engine)
    cid = _seed_channel(engine, pid)
    with Session(engine) as s:
        s.add(
            SetupChecklistItem(
                product_id=pid,
                channel_id=cid,
                ord=0,
                instruction="Make account",
                category="account",
            )
        )
        s.commit()

    chans = c.get(f"/api/private/channels/{pid}").json()
    assert len(chans) == 1 and chans[0]["type"] == "reddit"
    items = c.get(f"/api/private/channels/{pid}/checklist").json()
    assert len(items) == 1 and items[0]["category"] == "account"


# ---- OAuth connect ----------------------------------------------------------------------


def test_connect_stores_token_in_vault_and_marks_connected(ctx):
    c, engine = ctx
    pid = _seed_product(engine)
    cid = _seed_channel(engine, pid)

    resp = c.post(
        f"/api/private/channels/{pid}/{cid}/connect",
        json={"access_token": "tok-abc", "refresh_token": "ref-xyz", "account_ref": "@auto"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["connect_state"] == ConnectState.CONNECTED
    assert body["account_ref"] == "@auto"

    # token is retrievable + encrypted at rest (channel-scoped)
    with Session(engine) as s:
        assert vault.get_credential(s, pid, "reddit_oauth", channel_id=cid) == "tok-abc"
        assert vault.get_credential(s, pid, "reddit_oauth_refresh", channel_id=cid) == "ref-xyz"
        from sqlmodel import select

        from app.models import Credential

        cred = s.exec(select(Credential).where(Credential.key == "reddit_oauth")).first()
        assert "tok-abc" not in cred.ciphertext  # only ciphertext at rest


def test_connect_empty_token_400(ctx):
    c, engine = ctx
    pid = _seed_product(engine)
    cid = _seed_channel(engine, pid)
    assert (
        c.post(f"/api/private/channels/{pid}/{cid}/connect", json={"access_token": ""}).status_code
        == 400
    )


def test_connect_wrong_channel_404(ctx):
    c, engine = ctx
    pid = _seed_product(engine)
    assert (
        c.post(f"/api/private/channels/{pid}/999/connect", json={"access_token": "t"}).status_code
        == 404
    )


# ---- checklist toggle -------------------------------------------------------------------


def test_toggle_checklist_item(ctx):
    c, engine = ctx
    pid = _seed_product(engine)
    with Session(engine) as s:
        item = SetupChecklistItem(
            product_id=pid, channel_id=None, ord=0, instruction="DNS", category="dns"
        )
        s.add(item)
        s.commit()
        s.refresh(item)
        item_id = item.id

    resp = c.patch(f"/api/private/channels/{pid}/checklist/{item_id}", json={"status": "done"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"


def test_toggle_missing_item_404(ctx):
    c, engine = ctx
    pid = _seed_product(engine)
    assert (
        c.patch(f"/api/private/channels/{pid}/checklist/999", json={"status": "done"}).status_code
        == 404
    )
