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
