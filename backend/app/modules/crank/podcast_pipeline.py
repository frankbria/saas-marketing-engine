"""Podcast music-mix tick: dispatch to / collect from the GPU `media` queue (S5.2, #30).

The bridge between the two Phase B execution planes for the *music-bed* podcast path only. A
narration-only episode never reaches here — it is finalized in-process by generate_podcast.py — so
this tick's cold path (no music-bed `rendering` items) touches neither the broker nor the workspace,
which is what keeps a bedless episode at zero GPU minutes.

Music-bed `rendering` items (produced by generate_podcast.py with their narration checkpointed) are
dispatched as `media.render_audio` Celery tasks; the finished mixed MP3 is collected back into the
workspace as `media_ref` + promoted to `critic_passed`, from where the existing S4.5 pace/publish
machinery takes over untouched.

Same contract as the video tick (S5.1): the task is a pure function of its args so it parks on the
broker until the provisioner boots a pod, `acks_late`/`reject_on_worker_lost` requeue it on pod
loss, and a task that keeps failing is re-dispatched at most `podcast_max_render_dispatches` times
before the item fails `render_failed` (never stranded in `rendering`). Never raises (scheduler-tick
contract); each item commits independently.

Known limitation (v1): the mixed MP3 rides back through the Celery result backend (Redis)
base64-encoded, capped by `podcast_render_max_bytes` — an object store can replace the transfer
inside `send`/`poll` without touching callers (same v1 limit as the video render).
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from app.config import settings
from app.models import ContentItem
from app.models.content_item import ContentItemStatus
from app.modules.crank.crank import ContentType
from app.modules.crank.generate_podcast import NARRATION_FILE, _atomic_write

logger = logging.getLogger(__name__)

EPISODE_FILE = "episode.mp3"

# send(narration_b64, music_prompt, max_bytes) -> task_id — the Celery boundary, injected in tests.
SendFn = Callable[[str, str, int], str]
# poll(task_id) -> ("pending", None) | ("success", result_b64) | ("failed", error_text)
PollFn = Callable[[str], tuple[str, str | None]]


def _real_send(narration_b64: str, music_prompt: str, max_bytes: int) -> str:
    # send_task by name (not a task import): the VPS process must not import render code meant for
    # the GPU image, and `media.*` names route to the media queue by celery_app.task_routes.
    from app.celery_app import celery_app

    result = celery_app.send_task(
        "media.render_audio", args=[narration_b64, music_prompt], kwargs={"max_bytes": max_bytes}
    )
    return result.id


def _real_poll(task_id: str) -> tuple[str, str | None]:
    from celery.result import AsyncResult

    from app.celery_app import celery_app

    result = AsyncResult(task_id, app=celery_app)
    if result.successful():
        return ("success", result.result)
    if result.failed():
        return ("failed", str(result.result))
    return ("pending", None)  # queued (cold-start), running, or retrying — all "not yet"


def advance_podcast_renders(
    session: Session, now: datetime, *, send: SendFn = _real_send, poll: PollFn = _real_poll
) -> None:
    """Advance every music-bed `rendering` podcast item one step: dispatch if undispatched, collect
    if finished, re-dispatch (bounded) if failed. Scoped to podcast items so it never touches a
    video render. Never raises — scheduler-tick contract."""
    items = session.exec(
        select(ContentItem).where(
            ContentItem.status == ContentItemStatus.RENDERING,
            ContentItem.content_type == ContentType.PODCAST.value,
        )
    ).all()
    for item in items:
        try:
            _advance_one(session, item, send=send, poll=poll)
        except Exception:  # noqa: BLE001 — one bad item must not stop the tick (S4.5 convention)
            logger.exception("podcast render tick failed for content_item %s", item.id)
            session.rollback()


def _advance_one(session: Session, item: ContentItem, *, send: SendFn, poll: PollFn) -> None:
    meta = json.loads(item.meta_json or "{}")
    render = meta.setdefault("render", {})
    last_error: str | None = None

    task_id = render.get("task_id")
    if task_id is not None:
        state, payload = poll(task_id)
        if state == "pending":
            return  # parked on the broker (cold-start) or still mixing — check next tick
        if state == "success":
            data = base64.b64decode(payload or "")
            if len(data) <= settings.podcast_render_max_bytes:
                _collect(session, item, meta, data)
                return
            # Defense in depth: the task guards its output size on the pod, but the collect side
            # must also refuse a result that would blow the workspace (a lying/old worker).
            last_error = f"mix result exceeds podcast_render_max_bytes ({len(data)} bytes)"
        else:
            last_error = payload or "audio mix task failed"
        render.pop("task_id")  # failed/oversized → eligible for re-dispatch below

    dispatches = render.get("dispatches", 0)
    if dispatches >= settings.podcast_max_render_dispatches:
        item.status = ContentItemStatus.RENDER_FAILED  # terminal — never strand in `rendering`
        item.error = f"audio mix failed after {dispatches} dispatches: {last_error}"
        item.meta_json = json.dumps(meta)
        session.add(item)
        session.commit()
        return

    podcast_dir = Path(settings.workspace_root) / meta["podcast_dir"]
    narration_b64 = base64.b64encode((podcast_dir / NARRATION_FILE).read_bytes()).decode()
    music_prompt = meta.get("music_prompt", "")
    # Count the dispatch in its own commit BEFORE sending: a crash after send would otherwise lose
    # both counter and task id, letting a crash-looping tick enqueue unbounded paid GPU work.
    # Pre-counting makes `dispatches` a true upper bound on sends (mirrors the video tick).
    render["dispatches"] = dispatches + 1
    item.meta_json = json.dumps(meta)
    session.add(item)
    session.commit()
    render["task_id"] = send(narration_b64, music_prompt, settings.podcast_render_max_bytes)
    item.meta_json = json.dumps(meta)
    session.add(item)
    session.commit()


def _collect(session: Session, item: ContentItem, meta: dict, data: bytes) -> None:
    """Land the finished mixed MP3 in the workspace and hand the item to the S4.5 pipeline."""
    podcast_dir = Path(settings.workspace_root) / meta["podcast_dir"]
    podcast_dir.mkdir(parents=True, exist_ok=True)  # survives a wiped workspace on a restored DB
    _atomic_write(podcast_dir / EPISODE_FILE, data)
    item.media_ref = f"{meta['podcast_dir']}/{EPISODE_FILE}"
    item.status = ContentItemStatus.CRITIC_PASSED  # gates already ran on the script (S4.3/S4.4)
    item.error = None
    item.meta_json = json.dumps(meta)
    session.add(item)
    session.commit()
