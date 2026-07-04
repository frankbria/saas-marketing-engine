"""Video render tick: dispatch to / collect from the GPU `media` queue (S5.1, #29).

The bridge between the two Phase B execution planes: `rendering` items (produced by
generate_video.py with their inputs checkpointed in the workspace) are dispatched as
`media.render_video` Celery tasks, and finished renders are collected back into the workspace as
`media_ref` + promoted to `critic_passed` — from where the existing S4.5 pace/publish machinery
takes over untouched.

Cold-start + teardown (AC): the task itself is a pure function of its args, so it parks on the
broker until the S5.0 provisioner boots a pod, and `acks_late`/`reject_on_worker_lost` requeue it
if the pod dies mid-render — nothing here needs to know. What this tick *does* own is the terminal
failure path: a task that keeps failing is re-dispatched at most `video_max_render_dispatches`
times, then the item fails `render_failed` instead of stranding in `rendering` forever.

Same never-raise contract as the other scheduler ticks (one bad item must not stop the rest), and
each item commits independently (crash isolation, mirrors publish_scheduled).

Known limitation (v1): the MP4 rides back through the Celery result backend (Redis) base64-encoded,
capped by `video_render_max_bytes` — fine for short-form sizes at v1 scale; an object store can
replace the transfer inside `send`/`poll` without touching callers. A result the broker *loses*
outright (Redis flush) leaves the task `pending` indefinitely — the acks_late requeue covers worker
loss, not broker loss; operator retract/re-crank covers the rest at v1 scale.
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
from app.modules.crank.generate_video import NARRATION_FILE, SCRIPT_FILE, _atomic_write

logger = logging.getLogger(__name__)

RENDERED_FILE = "final.mp4"

# send(script, narration_b64, max_bytes) -> task_id — the Celery boundary, injected in tests.
SendFn = Callable[[dict, str, int], str]
# poll(task_id) -> ("pending", None) | ("success", result_b64) | ("failed", error_text)
PollFn = Callable[[str], tuple[str, str | None]]


def _real_send(script: dict, narration_b64: str, max_bytes: int) -> str:
    # send_task by name (not a task import): the VPS process must not import render code meant
    # for the GPU image, and `media.*` names route to the media queue by celery_app.task_routes.
    from app.celery_app import celery_app

    result = celery_app.send_task(
        "media.render_video", args=[script, narration_b64], kwargs={"max_bytes": max_bytes}
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


def advance_video_renders(
    session: Session, now: datetime, *, send: SendFn = _real_send, poll: PollFn = _real_poll
) -> None:
    """Advance every `rendering` item one step: dispatch if undispatched, collect if finished,
    re-dispatch (bounded) if failed. Never raises — scheduler-tick contract."""
    items = session.exec(
        select(ContentItem).where(ContentItem.status == ContentItemStatus.RENDERING)
    ).all()
    for item in items:
        try:
            _advance_one(session, item, send=send, poll=poll)
        except Exception:  # noqa: BLE001 — one bad item must not stop the tick (S4.5 convention)
            logger.exception("video render tick failed for content_item %s", item.id)
            session.rollback()


def _advance_one(session: Session, item: ContentItem, *, send: SendFn, poll: PollFn) -> None:
    meta = json.loads(item.meta_json or "{}")
    render = meta.setdefault("render", {})
    last_error: str | None = None

    task_id = render.get("task_id")
    if task_id is not None:
        state, payload = poll(task_id)
        if state == "pending":
            return  # parked on the broker (cold-start) or still rendering — check next tick
        if state == "success":
            data = base64.b64decode(payload or "")
            if len(data) <= settings.video_render_max_bytes:
                _collect(session, item, meta, data)
                return
            # Defense in depth: the task guards its output size on the pod, but the collect side
            # must also refuse a result that would blow the workspace (a lying/old worker).
            last_error = f"render result exceeds video_render_max_bytes ({len(data)} bytes)"
        else:
            last_error = payload or "render task failed"
        render.pop("task_id")  # failed/oversized → eligible for re-dispatch below

    dispatches = render.get("dispatches", 0)
    if dispatches >= settings.video_max_render_dispatches:
        item.status = ContentItemStatus.RENDER_FAILED  # terminal — never strand in `rendering`
        item.error = f"render failed after {dispatches} dispatches: {last_error}"
        item.meta_json = json.dumps(meta)
        session.add(item)
        session.commit()
        return

    video_dir = Path(settings.workspace_root) / meta["video_dir"]
    script = json.loads((video_dir / SCRIPT_FILE).read_text())["script"]
    narration_b64 = base64.b64encode((video_dir / NARRATION_FILE).read_bytes()).decode()
    # Count the dispatch in its own commit BEFORE sending: a crash after send would otherwise
    # lose both counter and task id, letting a crash-looping tick enqueue unbounded paid GPU
    # work. Pre-counting makes `dispatches` a true upper bound on sends; the worst case flips
    # to a burned attempt with no task, which the bound already prices in. (A crash between
    # send and the task-id commit still orphans that one task — the result sits unread; fully
    # closing that needs a transactional outbox, deliberately out of v1 scope.)
    render["dispatches"] = dispatches + 1
    item.meta_json = json.dumps(meta)
    session.add(item)
    session.commit()
    render["task_id"] = send(script, narration_b64, settings.video_render_max_bytes)
    item.meta_json = json.dumps(meta)
    session.add(item)
    session.commit()


def _collect(session: Session, item: ContentItem, meta: dict, data: bytes) -> None:
    """Land the finished MP4 in the workspace and hand the item to the S4.5 pipeline."""
    video_dir = Path(settings.workspace_root) / meta["video_dir"]
    video_dir.mkdir(parents=True, exist_ok=True)  # survives a wiped workspace on a restored DB
    _atomic_write(video_dir / RENDERED_FILE, data)
    item.media_ref = f"{meta['video_dir']}/{RENDERED_FILE}"
    item.status = ContentItemStatus.CRITIC_PASSED  # gates already ran on the script (S4.3/S4.4)
    item.error = None
    item.meta_json = json.dumps(meta)
    session.add(item)
    session.commit()
