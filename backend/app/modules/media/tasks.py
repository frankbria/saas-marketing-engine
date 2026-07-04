"""Celery tasks on the `media` queue (S5.0).

`media.probe` is the queue's original noop, proving the enqueue → broker → GPU-worker round-trip
end-to-end (the cold-start acceptance test drives it). Real media tasks register here with the same
`media.` name prefix, which is what routes them to the GPU queue (see app/celery_app.py
task_routes): `media.render_video` (S5.1) renders short-form MP4s, `media.render_audio` (S5.2)
mixes an optional podcast music bed under the narration.
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


@celery_app.task(
    name="media.render_audio",
    acks_late=True,
    max_retries=MAX_ATTEMPTS - 1,
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def render_audio(narration_b64: str, music_prompt: str, max_bytes: int) -> str:
    """Mix a podcast music bed under the narration on the GPU pod, returning it base64 (S5.2, #30).

    Thin task wrapper: the real work is the pure `app.modules.media.audio.render_audio` (music-bed
    generation + ffmpeg mix), so the pod needs no DB/settings. `max_bytes` is passed in by the
    caller (the VPS resolves settings.podcast_render_max_bytes) — the GPU worker must not read VPS
    config. Imported lazily to keep this module import-light (no ffmpeg/model deps at registration
    time). Only music-bed episodes reach this task; narration-only episodes finish on the VPS.
    """
    from app.modules.media.audio import render_audio as _mix

    return _mix(narration_b64, music_prompt, max_bytes=max_bytes)
