"""Celery app for Phase B media jobs (S5.0, TECH_SPEC Phase B decision 2026-07-03).

Only the dedicated `media` queue rides Celery — long GPU media jobs (video S5.1, podcast
S5.2) consumed by the ephemeral rented GPU worker, which connects OUT to the VPS Redis.
The text/blog crank stays on the in-process worker loop (issue #28 non-goals); it migrates
only when there's a reason.

The GPU worker process runs `celery -A app.celery_app worker -Q media` from the pinned
image in infra/gpu-worker. `include=` (not an import) registers the task modules so this
module stays import-cycle-free.
"""

from celery import Celery

from app.config import settings

# The queue name is deliberately a constant, not a setting: the provisioner, the worker
# image CMD, and the routing table must all agree on it, and there is exactly one.
MEDIA_QUEUE = "media"

celery_app = Celery(
    "sme",
    broker=settings.celery_broker_url,
    # Results land on the broker Redis so callers (and Flower) can see job outcomes —
    # "retries/visibility" is an S5.0 acceptance criterion.
    backend=settings.celery_broker_url,
    include=["app.modules.media.tasks"],
)

celery_app.conf.update(
    task_default_queue="default",
    # Route by task-name namespace: every media.* task lands on the GPU queue. A mis-routed
    # media job would run where there is no GPU; a non-media job on `media` would boot a
    # paid pod for nothing.
    task_routes={"media.*": {"queue": MEDIA_QUEUE}},
    # acks_late + prefetch 1: a pod killed mid-job (idle teardown race, spot loss) re-delivers
    # the message to the next worker instead of silently dropping it.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    # UTC everywhere, matching the rest of the app.
    enable_utc=True,
)
