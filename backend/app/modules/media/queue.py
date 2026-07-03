"""Broker-side introspection of the `media` queue (S5.0).

The provisioner's boot/teardown decisions key off two observations: is work pending
(queue depth) and is a worker alive (celery ping). Both talk to the real broker; the
orchestrator takes them as injectable callables so its state machine tests stay
deterministic without mocking Redis.
"""

import redis

from app.celery_app import MEDIA_QUEUE, celery_app
from app.config import settings


def media_queue_depth() -> int:
    """Pending (undelivered) messages on the media queue. Celery's Redis transport keeps
    a queue as a plain list keyed by queue name, so depth is just LLEN."""
    client = redis.Redis.from_url(settings.celery_broker_url)
    return int(client.llen(MEDIA_QUEUE))


def media_worker_online(timeout: float = 1.0) -> bool:
    """True if any Celery worker consuming the media queue answers a ping."""
    replies = celery_app.control.ping(timeout=timeout) or []
    return len(replies) > 0
