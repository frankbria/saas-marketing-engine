"""Celery tasks on the `media` queue (S5.0).

v1 ships only `media.probe` — the queue's noop, proving the enqueue → broker → GPU-worker
round-trip end-to-end (the cold-start acceptance test drives it). Real media tasks
(video S5.1, podcast S5.2) register here with the same `media.` name prefix, which is
what routes them to the GPU queue (see app/celery_app.py task_routes).
"""

from app.celery_app import celery_app
from app.worker import MAX_ATTEMPTS


@celery_app.task(
    name="media.probe",
    acks_late=True,
    # max_retries counts re-deliveries: first run + retries == MAX_ATTEMPTS, the same
    # contract as the in-process worker loop.
    max_retries=MAX_ATTEMPTS - 1,
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def probe(payload: str) -> str:
    """Echo the payload. Exists to prove queue plumbing, not to do work."""
    return payload


@celery_app.task(
    name="media.render_video",
    acks_late=True,
    max_retries=MAX_ATTEMPTS - 1,
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def render_video(script: dict, narration_b64: str, max_bytes: int) -> str:
    """Render the short-form MP4 on the GPU pod, returning it base64-encoded (S5.1, #29).

    Thin task wrapper: the real work is the pure `app.modules.media.video.render_video`, so
    the pod needs no DB/settings. `max_bytes` is passed in by the caller (the VPS resolves
    settings.video_render_max_bytes) — the GPU worker must not read VPS config. Imported
    lazily to keep this module import-light (no ffmpeg/heavy deps at registration time).
    """
    from app.modules.media.video import render_video as _render

    return _render(script, narration_b64, max_bytes=max_bytes)
