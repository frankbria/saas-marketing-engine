"""S2.6/S4.8.1: channels API — setup trigger gates, list endpoints, OAuth connect (real vault),
per-provider credential shape (self-managed Reddit vs owned bare token), toggles."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.channels.reddit import RedditAdapter
from app.db import get_session
from app.main import create_app
from app.models import (
    Channel,
    ChannelType,
    ConnectState,
    ContentItem,
    ContentItemStatus,
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


# ---- OAuth connect (S4.8.1: per-provider credential shape) -------------------------------

_PRAW = {"client_id": "cid", "client_secret": "sec", "refresh_token": "rt", "user_agent": "ua"}


def test_connect_reddit_stores_praw_kwargs(ctx):
    """Reddit is self-managed (PRAW): the documented shape is a PRAW-kwargs JSON blob, stored
    under reddit_oauth so RedditAdapter._parse_creds can consume it (AC1/AC2)."""
    c, engine = ctx
    pid = _seed_product(engine)
    cid = _seed_channel(engine, pid, ctype=ChannelType.REDDIT)

    resp = c.post(
        f"/api/private/channels/{pid}/{cid}/connect",
        json={"reddit": _PRAW, "account_ref": "u/auto"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["connect_state"] == ConnectState.CONNECTED
    assert body["account_ref"] == "u/auto"

    with Session(engine) as s:
        stored = vault.get_credential(s, pid, "reddit_oauth", channel_id=cid)
        # the stored value is exactly the PRAW-kwargs JSON object the adapter parses
        assert json.loads(stored) == _PRAW
        # self-managed: no separate bare-token refresh credential is written (PRAW self-refreshes)
        assert vault.get_credential(s, pid, "reddit_oauth_refresh", channel_id=cid) is None

        from sqlmodel import select

        from app.models import Credential

        cred = s.exec(select(Credential).where(Credential.key == "reddit_oauth")).first()
        assert "cid" not in cred.ciphertext  # only ciphertext at rest


def test_connect_reddit_missing_creds_400(ctx):
    """A self-managed channel with no `reddit` block is rejected — no bare-token footgun."""
    c, engine = ctx
    pid = _seed_product(engine)
    cid = _seed_channel(engine, pid, ctype=ChannelType.REDDIT)
    assert (
        c.post(
            f"/api/private/channels/{pid}/{cid}/connect", json={"access_token": "tok"}
        ).status_code
        == 400
    )


def test_connect_reddit_blank_field_rejected(ctx):
    """A blank/whitespace PRAW field is rejected (422) rather than stored as a connected-but-broken
    credential — parity with the owned path's empty-token guard."""
    c, engine = ctx
    pid = _seed_product(engine)
    cid = _seed_channel(engine, pid, ctype=ChannelType.REDDIT)
    creds = {**_PRAW, "client_id": "   "}
    assert (
        c.post(f"/api/private/channels/{pid}/{cid}/connect", json={"reddit": creds}).status_code
        == 422
    )


def test_connect_owned_token_stores_bare(ctx):
    """Owned (bare-token) providers keep the access_token/refresh_token path unchanged."""
    c, engine = ctx
    pid = _seed_product(engine)
    cid = _seed_channel(engine, pid, ctype=ChannelType.X)

    resp = c.post(
        f"/api/private/channels/{pid}/{cid}/connect",
        json={"access_token": "tok-abc", "refresh_token": "ref-xyz", "account_ref": "@auto"},
    )
    assert resp.status_code == 200
    assert resp.json()["connect_state"] == ConnectState.CONNECTED

    with Session(engine) as s:
        assert vault.get_credential(s, pid, "x_oauth", channel_id=cid) == "tok-abc"
        assert vault.get_credential(s, pid, "x_oauth_refresh", channel_id=cid) == "ref-xyz"


def test_connect_owned_empty_token_400(ctx):
    c, engine = ctx
    pid = _seed_product(engine)
    cid = _seed_channel(engine, pid, ctype=ChannelType.X)
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


class _FakeSubmission:
    permalink = "/r/test/comments/xyz/hi/"


class _FakeSubreddit:
    def submit(self, *, title, selftext, flair_id):
        return _FakeSubmission()


class _FakeReddit:
    """Minimal PRAW stand-in: no prior submissions, records nothing but a successful submit."""

    def __init__(self):
        self.user = SimpleNamespace(
            me=lambda: SimpleNamespace(submissions=SimpleNamespace(new=lambda limit=None: []))
        )

    def subreddit(self, name):
        return _FakeSubreddit()


def test_connect_reddit_then_publish_end_to_end(ctx, monkeypatch):
    """AC3: a Reddit channel connected via the documented /connect flow publishes end-to-end —
    real vault round-trip + a fake PRAW client. Proves the stored shape is exactly what the
    adapter builds its client from."""
    c, engine = ctx
    pid = _seed_product(engine)
    cid = _seed_channel(engine, pid, ctype=ChannelType.REDDIT)

    resp = c.post(f"/api/private/channels/{pid}/{cid}/connect", json={"reddit": _PRAW})
    assert resp.status_code == 200

    captured: dict = {}
    monkeypatch.setattr(
        "app.channels.reddit._build_reddit",
        lambda creds: (captured.update(creds=creds), _FakeReddit())[1],
    )

    with Session(engine) as s:
        product = s.get(Product, pid)
        channel = s.get(Channel, cid)
        channel.profile_json = json.dumps({"subreddit": "test"})
        s.add(channel)
        item = ContentItem(
            product_id=pid,
            channel_id=cid,
            content_type="reddit",
            status=ContentItemStatus.SCHEDULED,
            title="Launch",
            body="value first",
            idempotency_key="reddit:1",
        )
        s.add(item)
        s.commit()
        s.refresh(item)
        s.refresh(channel)

        creds = vault.get_credential(s, pid, "reddit_oauth", channel_id=cid)
        result = RedditAdapter().publish(item, product, channel, creds)

    assert result.external_url == "https://www.reddit.com/r/test/comments/xyz/hi/"
    # the client was built from exactly the PRAW kwargs we connected with — shape round-trips
    assert captured["creds"] == _PRAW


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


# ---- pause / resume kill switch (S4.6) --------------------------------------------------


def test_pause_and_resume_channel(ctx):
    c, engine = ctx
    pid = _seed_product(engine)
    cid = _seed_channel(engine, pid)

    resp = c.patch(f"/api/private/channels/{pid}/{cid}/pause", json={"paused": True})
    assert resp.status_code == 200
    assert resp.json()["paused"] is True

    resp = c.patch(f"/api/private/channels/{pid}/{cid}/pause", json={"paused": False})
    assert resp.status_code == 200
    assert resp.json()["paused"] is False


def test_pause_wrong_channel_404(ctx):
    c, engine = ctx
    pid = _seed_product(engine)
    assert (
        c.patch(f"/api/private/channels/{pid}/999/pause", json={"paused": True}).status_code == 404
    )
