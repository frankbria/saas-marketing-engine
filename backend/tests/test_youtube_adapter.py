"""S5.1: YouTube publish adapter (Data API v3, resumable upload) with idempotency + retraction.

Drives `YouTubeAdapter` directly against a real SQLite file and a fake YouTube Data API served
through `httpx.MockTransport` — no network, no mocking of the code under test. The injectable
`app.channels.youtube._build_youtube` seam is monkeypatched to return a client wired to the fake,
mirroring the `_build_reddit` seam house style. A tmp workspace holds a small fake MP4 that
`item.media_ref` points at.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.channels.base import AuthFailure, PublishResult, Retryable, get_adapter
from app.channels.youtube import YouTubeAdapter
from app.config import settings
from app.models import Channel, ChannelType, ContentItem, Product

NOW = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)


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


def _channel(session, product_id, *, ctype=ChannelType.YOUTUBE):
    c = Channel(product_id=product_id, type=ctype, enabled=True, autonomous=True)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _item(session, product_id, channel_id, *, title="My Clip", body="Watch this", media_ref=None):
    it = ContentItem(
        product_id=product_id,
        channel_id=channel_id,
        content_type="video",
        title=title,
        body=body,
        media_ref=media_ref,
        created_at=NOW,
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


@pytest.fixture
def video_item(session, tmp_path, monkeypatch):
    """A YouTube channel + a content item whose media_ref resolves to a real (tiny) MP4 file."""
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path))
    media_rel = "acme/media/clip.mp4"
    media_abs = tmp_path / media_rel
    media_abs.parent.mkdir(parents=True)
    media_abs.write_bytes(b"\x00\x00fake-mp4-bytes")
    p = _product(session)
    c = _channel(session, p.id)
    it = _item(session, p.id, c.id, media_ref=media_rel)
    it.idempotency_key = "youtube:1"  # set by the pace pass in production
    return SimpleNamespace(product=p, channel=c, item=it, media_bytes=media_abs.read_bytes())


class _FakeYouTubeApi:
    """A hand-built YouTube Data API v3, served through httpx.MockTransport. Records every call and
    switches on URL/method: search + videos.list (idempotency scan), resumable upload init (POST →
    Location header) + the byte PUT, and video delete. Per-endpoint status codes let each test drive
    the error-classification split."""

    def __init__(
        self,
        *,
        existing=None,
        video_id="NEWVID",
        search_status=200,
        init_status=200,
        put_status=200,
        delete_status=204,
        error_reason="backendError",
    ):
        self.existing = existing or []  # list of (video_id, description)
        self.video_id = video_id
        self.search_status = search_status
        self.init_status = init_status
        self.put_status = put_status
        self.delete_status = delete_status
        self.error_reason = error_reason
        self.calls: list[tuple[str, str]] = []
        self.init_body: dict | None = None
        self.uploaded: bytes | None = None

    def _err(self, status):
        body = {"error": {"errors": [{"reason": self.error_reason}], "message": "boom"}}
        return httpx.Response(status, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, url = request.method, str(request.url)
        self.calls.append((method, url))
        if "youtube/v3/search" in url:
            if self.search_status != 200:
                return self._err(self.search_status)
            items = [{"id": {"videoId": vid}} for vid, _ in self.existing]
            return httpx.Response(200, json={"items": items})
        if "youtube/v3/videos" in url and method == "GET":
            items = [{"id": vid, "snippet": {"description": desc}} for vid, desc in self.existing]
            return httpx.Response(200, json={"items": items})
        if "upload/youtube/v3/videos" in url and method == "POST":
            if self.init_status != 200:
                return self._err(self.init_status)
            self.init_body = json.loads(request.content)
            return httpx.Response(200, headers={"Location": "https://uploads.example/session/abc"})
        if method == "PUT":
            if self.put_status != 200:
                return self._err(self.put_status)
            self.uploaded = request.content
            return httpx.Response(200, json={"id": self.video_id})
        if "youtube/v3/videos" in url and method == "DELETE":
            return httpx.Response(self.delete_status)
        return httpx.Response(404, json={"error": {"message": "unhandled"}})


def _install(monkeypatch, api: _FakeYouTubeApi):
    monkeypatch.setattr(
        "app.channels.youtube._build_youtube",
        lambda creds: httpx.Client(
            transport=httpx.MockTransport(api.handler),
            headers={"Authorization": f"Bearer {creds}"},
        ),
    )


def _install_transport_error(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("no route to host", request=request)

    monkeypatch.setattr(
        "app.channels.youtube._build_youtube",
        lambda creds: httpx.Client(transport=httpx.MockTransport(boom)),
    )


# --- publish: happy path + idempotency ---------------------------------------------------------


def test_publish_uploads_and_returns_watch_url(video_item, monkeypatch):
    api = _FakeYouTubeApi(video_id="ABC123")
    _install(monkeypatch, api)

    r = YouTubeAdapter().publish(video_item.item, video_item.product, video_item.channel, "tok")

    assert isinstance(r, PublishResult)
    assert r.external_url == "https://www.youtube.com/watch?v=ABC123"
    # resumable init POST + byte PUT both happened, with the real file bytes.
    assert any(m == "POST" and "upload/youtube/v3/videos" in u for m, u in api.calls)
    assert api.uploaded == video_item.media_bytes
    # description carries the idempotency marker; snippet/status set as spec'd.
    assert "sme-ref:youtube:1" in api.init_body["snippet"]["description"]
    assert api.init_body["snippet"]["title"] == "My Clip"
    assert api.init_body["snippet"]["categoryId"] == "22"
    assert api.init_body["status"]["privacyStatus"] == "public"
    assert api.init_body["status"]["selfDeclaredMadeForKids"] is False


def test_publish_idempotent_returns_existing_without_upload(video_item, monkeypatch):
    # A prior attempt already uploaded this item (its marker is on a remote video); the scan must
    # find it by idempotency_key and return its watch URL, never double-uploading.
    api = _FakeYouTubeApi(existing=[("OLDVID", "some copy\n\nsme-ref:youtube:1")])
    _install(monkeypatch, api)

    r = YouTubeAdapter().publish(video_item.item, video_item.product, video_item.channel, "tok")

    assert r.external_url == "https://www.youtube.com/watch?v=OLDVID"
    assert api.uploaded is None
    assert not any(m == "POST" and "upload" in u for m, u in api.calls)


def test_publish_same_title_different_key_does_not_dedup(video_item, monkeypatch):
    # A remote video carries a DIFFERENT item's marker (youtube:999) but the same title. The new
    # item (youtube:1) must still upload — dedup keys on idempotency_key, not the title.
    api = _FakeYouTubeApi(
        existing=[("OLDVID", "old copy\n\nsme-ref:youtube:999")], video_id="FRESH"
    )
    _install(monkeypatch, api)

    r = YouTubeAdapter().publish(video_item.item, video_item.product, video_item.channel, "tok")

    assert r.external_url == "https://www.youtube.com/watch?v=FRESH"
    assert api.uploaded == video_item.media_bytes


def test_publish_truncates_title_to_100_chars(video_item, monkeypatch):
    api = _FakeYouTubeApi()
    _install(monkeypatch, api)
    video_item.item.title = "x" * 250

    YouTubeAdapter().publish(video_item.item, video_item.product, video_item.channel, "tok")

    assert len(api.init_body["snippet"]["title"]) == 100


# --- publish: fail-closed guards (permanent, no HTTP) ------------------------------------------


def test_publish_missing_idempotency_key_is_permanent(video_item, monkeypatch):
    api = _FakeYouTubeApi()
    _install(monkeypatch, api)
    video_item.item.idempotency_key = None

    with pytest.raises(RuntimeError):
        YouTubeAdapter().publish(video_item.item, video_item.product, video_item.channel, "tok")
    assert api.calls == []  # fail closed — never touched the network


def test_publish_missing_media_ref_is_permanent(video_item, monkeypatch):
    api = _FakeYouTubeApi()
    _install(monkeypatch, api)
    video_item.item.media_ref = None

    with pytest.raises(RuntimeError):
        YouTubeAdapter().publish(video_item.item, video_item.product, video_item.channel, "tok")
    assert api.calls == []


def test_publish_missing_media_file_is_permanent(video_item, monkeypatch):
    api = _FakeYouTubeApi()
    _install(monkeypatch, api)
    video_item.item.media_ref = "acme/media/gone.mp4"

    with pytest.raises(RuntimeError):
        YouTubeAdapter().publish(video_item.item, video_item.product, video_item.channel, "tok")
    assert api.calls == []


# --- publish: error classification ------------------------------------------------------------


def test_publish_transport_error_is_retryable(video_item, monkeypatch):
    _install_transport_error(monkeypatch)

    with pytest.raises(Retryable):
        YouTubeAdapter().publish(video_item.item, video_item.product, video_item.channel, "tok")


def test_publish_server_error_on_init_is_retryable(video_item, monkeypatch):
    api = _FakeYouTubeApi(init_status=500)
    _install(monkeypatch, api)

    with pytest.raises(Retryable):
        YouTubeAdapter().publish(video_item.item, video_item.product, video_item.channel, "tok")


def test_publish_401_is_auth_failure(video_item, monkeypatch):
    api = _FakeYouTubeApi(search_status=401)
    _install(monkeypatch, api)

    with pytest.raises(AuthFailure):
        YouTubeAdapter().publish(video_item.item, video_item.product, video_item.channel, "tok")


def test_publish_403_quota_is_retryable(video_item, monkeypatch):
    api = _FakeYouTubeApi(search_status=403, error_reason="quotaExceeded")
    _install(monkeypatch, api)

    with pytest.raises(Retryable):
        YouTubeAdapter().publish(video_item.item, video_item.product, video_item.channel, "tok")


def test_publish_403_non_quota_is_permanent(video_item, monkeypatch):
    api = _FakeYouTubeApi(search_status=403, error_reason="forbidden")
    _install(monkeypatch, api)

    with pytest.raises(RuntimeError):
        YouTubeAdapter().publish(video_item.item, video_item.product, video_item.channel, "tok")


def test_publish_400_is_permanent(video_item, monkeypatch):
    api = _FakeYouTubeApi(init_status=400, error_reason="badRequest")
    _install(monkeypatch, api)

    with pytest.raises(RuntimeError):
        YouTubeAdapter().publish(video_item.item, video_item.product, video_item.channel, "tok")


# --- delete -----------------------------------------------------------------------------------


def test_delete_removes_video(video_item, monkeypatch):
    api = _FakeYouTubeApi(delete_status=204)
    _install(monkeypatch, api)

    YouTubeAdapter().delete(
        "https://www.youtube.com/watch?v=ABC123",
        video_item.product,
        video_item.channel,
        "tok",
    )

    assert any(m == "DELETE" and "id=ABC123" in u for m, u in api.calls)


def test_delete_on_404_is_noop(video_item, monkeypatch):
    api = _FakeYouTubeApi(delete_status=404)
    _install(monkeypatch, api)

    # already gone — idempotent no-op, must not raise
    YouTubeAdapter().delete(
        "https://www.youtube.com/watch?v=GONE",
        video_item.product,
        video_item.channel,
        "tok",
    )


# --- registry + owned-token registration ------------------------------------------------------


def test_get_adapter_returns_youtube_adapter():
    adapter = get_adapter(ChannelType.YOUTUBE)
    assert isinstance(adapter, YouTubeAdapter)
    assert adapter.type == ChannelType.YOUTUBE
    assert adapter.credential_key == "youtube_oauth"


def test_owned_token_providers_has_youtube_google_endpoints():
    from app.modules.crank.oauth_refresh import OWNED_TOKEN_PROVIDERS

    prov = OWNED_TOKEN_PROVIDERS[ChannelType.YOUTUBE]
    assert prov.authorize_url == "https://accounts.google.com/o/oauth2/v2/auth"
    assert prov.token_url == "https://oauth2.googleapis.com/token"
    assert "https://www.googleapis.com/auth/youtube.upload" in prov.scopes
    assert "https://www.googleapis.com/auth/youtube.readonly" in prov.scopes
