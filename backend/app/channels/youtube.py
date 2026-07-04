"""YouTube publish adapter via the YouTube Data API v3 (TECH_SPEC §7/§8.3, story S5.1).

The terminal step of the short-form video pipeline: a rendered MP4 (workspace-relative
`item.media_ref`) is uploaded as a public video with the Data API v3 resumable-upload protocol —
a POST that returns a session `Location`, then a byte PUT to that URL. `httpx` is used directly (no
google SDK), keeping the pinned dependency set unchanged; it is already a runtime dep via the media
provisioner.

`_build_youtube` is module-level so tests inject a fake client (matching the `_build_reddit` seam);
credentials arrive as a BARE access-token string (the owned-token shape `refresh_channel_token`
maintains), so the client just sets `Authorization: Bearer <token>`.

Idempotency (§8.3, keyed on `item.idempotency_key`): YouTube has no native idempotency key, so the
video description carries a small `sme-ref:{key}` marker; before uploading we scan our own recent
uploads (search → videos.list) for that marker and return the existing watch URL instead of
re-uploading — the §7 "check remote before re-post" rule, keyed on the idempotency key (not the
title, so two items sharing a title never collide). The DB status guard remains the primary defense.

Errors mirror reddit.py's transient/permanent split: httpx transport errors/timeouts → `Retryable`
(retry next tick); 5xx → `Retryable`; 401 → `AuthFailure` (a dead owned token — S4.8 fences the
channel); 403 → `Retryable` only when the body signals a quota/rate-limit reason, else permanent;
other 4xx → a permanent `RuntimeError` so the publish pass records `publish_failed` instead of
retrying a doomed upload forever.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

import httpx

from app.channels.base import AuthFailure, PublishResult, Retryable
from app.config import settings
from app.models import Channel, ContentItem, Product
from app.models.channel import ChannelType

# YouTube caps a video title at 100 characters (Data API v3 rejects longer); truncate to fit.
_TITLE_MAX = 100
# How many of the account's own recent uploads to scan for an existing video before re-uploading.
_RECENT_UPLOAD_SCAN = 50
# Generous timeouts: the byte PUT streams a whole MP4, but connect should fail fast.
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)

_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
# People & Blogs — a safe default category for autonomously published short-form clips.
_CATEGORY_ID = "22"


def _build_youtube(creds: str) -> httpx.Client:
    """Build an authorized httpx client from the bare `youtube_oauth` access token. Injected seam so
    tests swap in a MockTransport-backed client (no network)."""
    return httpx.Client(
        headers={"Authorization": f"Bearer {creds}", "Accept": "application/json"},
        timeout=_TIMEOUT,
    )


def _ref_marker(idempotency_key: str) -> str:
    """Stable per-item marker embedded in the description so the remote scan identifies the exact
    prior upload — keyed on `idempotency_key`, so two items with the same title never collide."""
    return f"sme-ref:{idempotency_key}"


def _description_with_marker(body: str, marker: str) -> str:
    return f"{body}\n\n{marker}"


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _video_id_from_url(external_url: str) -> str | None:
    """Parse a video id from a watch URL (`watch?v=<id>`), a `videos?id=<id>` API URL, or a
    `youtu.be/<id>` short link."""
    parsed = urllib.parse.urlparse(external_url)
    qs = urllib.parse.parse_qs(parsed.query)
    for key in ("v", "id"):
        if qs.get(key):
            return qs[key][0]
    tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return tail or None


def _is_quota(resp: httpx.Response) -> bool:
    """A 403 that signals quota/rate-limit (transient) rather than a hard permission denial. The
    Data API sets `error.errors[].reason` to `quotaExceeded`/`rateLimitExceeded` for these; a plain
    body substring match keeps it defensive against shape drift."""
    text = resp.text.lower()
    return "quota" in text or "ratelimit" in text


def _raise_for_status(resp: httpx.Response, action: str) -> None:
    """Classify a non-2xx YouTube response, mirroring reddit.py's split: 5xx → Retryable; 401 →
    AuthFailure (dead owned token); 403 → Retryable only on a quota/rate-limit reason, else
    permanent; other 4xx → permanent RuntimeError with a status + body snippet."""
    if resp.is_success:
        return
    status = resp.status_code
    snippet = resp.text[:500]
    if status >= 500:
        raise Retryable(f"youtube {action} failed: {status} {snippet}")
    if status == 401:
        raise AuthFailure(f"youtube {action} auth failed: {status} {snippet}")
    if status == 403 and _is_quota(resp):
        raise Retryable(f"youtube {action} rate-limited: {status} {snippet}")
    raise RuntimeError(f"youtube {action} failed: {status} {snippet}")


def _existing_watch_url(client: httpx.Client, marker: str) -> str | None:
    """Return the watch URL of an already-uploaded video carrying this item's ref marker in its
    description, or None — the "check remote before re-post" idempotency guard. Two calls: search
    for our own recent video ids, then read those videos' snippets and match the marker."""
    resp = client.get(
        _SEARCH_URL,
        params={
            "part": "id",
            "forMine": "true",
            "type": "video",
            "maxResults": _RECENT_UPLOAD_SCAN,
        },
    )
    _raise_for_status(resp, "search")
    ids = [
        item["id"]["videoId"]
        for item in resp.json().get("items", [])
        if item.get("id", {}).get("videoId")
    ]
    if not ids:
        return None
    resp = client.get(_VIDEOS_URL, params={"part": "snippet", "id": ",".join(ids)})
    _raise_for_status(resp, "videos")
    for video in resp.json().get("items", []):
        description = (video.get("snippet") or {}).get("description", "") or ""
        if marker in description:
            return _watch_url(video["id"])
    return None


def _resumable_upload(client: httpx.Client, media_path: Path, title: str, description: str) -> str:
    """Run the two-leg resumable upload and return the new video id. Leg 1 POSTs the snippet/status
    metadata and reads the session `Location`; leg 2 PUTs the file bytes to it."""
    metadata = {
        "snippet": {"title": title, "description": description, "categoryId": _CATEGORY_ID},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    init = client.post(
        _UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        json=metadata,
    )
    _raise_for_status(init, "upload-init")
    location = init.headers.get("Location")
    if not location:
        raise RuntimeError("youtube resumable upload init returned no Location header")
    put = client.put(location, content=media_path.read_bytes(), headers={"Content-Type": "video/*"})
    _raise_for_status(put, "upload")
    video_id = (put.json() or {}).get("id")
    if not video_id:
        raise RuntimeError(f"youtube upload response has no video id: {put.text[:200]}")
    return video_id


class YouTubeAdapter:
    type = ChannelType.YOUTUBE
    credential_key = "youtube_oauth"

    def publish(
        self, item: ContentItem, product: Product, channel: Channel, creds: str | None
    ) -> PublishResult:
        # Fail closed: a missing key/media is a broken upstream contract, not a transient error, and
        # a missing key would silently disable the remote idempotency guard on a non-idempotent
        # upload. All permanent — the publish pass records `publish_failed`, no retry.
        if not item.idempotency_key:
            raise RuntimeError(
                f"content_item {item.id} has no idempotency_key; refusing a non-idempotent "
                "youtube upload"
            )
        if not item.media_ref:
            raise RuntimeError(
                f"content_item {item.id} has no media_ref; nothing to upload to youtube"
            )
        media_path = Path(settings.workspace_root) / item.media_ref
        if not media_path.is_file():
            raise RuntimeError(
                f"content_item {item.id} media_ref {item.media_ref!r} does not resolve to a file"
            )

        marker = _ref_marker(item.idempotency_key)
        title = (item.title or item.body.splitlines()[0])[:_TITLE_MAX]
        description = _description_with_marker(item.body, marker)

        try:
            with _build_youtube(creds) as client:
                existing = _existing_watch_url(client, marker)
                if existing is not None:
                    return PublishResult(external_url=existing)
                video_id = _resumable_upload(client, media_path, title, description)
        except httpx.TransportError as exc:
            # Connect/read/timeout — transient, retry next tick.
            raise Retryable(f"youtube upload failed: {exc}") from exc
        return PublishResult(external_url=_watch_url(video_id))

    def delete(
        self, external_url: str, product: Product, channel: Channel, creds: str | None
    ) -> None:
        video_id = _video_id_from_url(external_url)
        if not video_id:
            raise RuntimeError(f"cannot parse a youtube video id from {external_url!r}")
        try:
            with _build_youtube(creds) as client:
                resp = client.delete(_VIDEOS_URL, params={"id": video_id})
                if resp.status_code == 404:
                    return  # already gone — idempotent no-op (retraction is best-effort)
                _raise_for_status(resp, "delete")
        except httpx.TransportError as exc:
            raise Retryable(f"youtube delete failed: {exc}") from exc
