"""S5.0: Celery `media` queue (issue #28).

Media jobs ride a dedicated Celery queue consumed only by the ephemeral GPU worker; the
text/blog crank stays on the in-process loop. Routing/retry config is unit-tested; the
broker round-trip (enqueue → depth → worker consumes) runs against a real Redis (repo
policy: no mocking of Redis/Celery) and skips when none is reachable — CI provides one.
"""

import pytest
import redis

from app.celery_app import MEDIA_QUEUE, celery_app
from app.config import settings
from app.modules.media.queue import media_queue_depth
from app.modules.media.tasks import probe
from app.worker import MAX_ATTEMPTS


def _redis_available() -> bool:
    try:
        redis.Redis.from_url(settings.celery_broker_url, socket_connect_timeout=1).ping()
        return True
    except (redis.exceptions.RedisError, OSError):
        return False


requires_redis = pytest.mark.skipif(
    not _redis_available(),
    reason="requires Redis at SME_CELERY_BROKER_URL (start infra/compose.dev.yml)",
)


def test_media_tasks_route_to_media_queue():
    # Every media.* task must land on the dedicated queue — a mis-routed media job would
    # run on the VPS (no GPU) instead of the rented worker.
    options = celery_app.amqp.router.route({}, "media.probe")
    assert options["queue"].name == MEDIA_QUEUE


def test_default_queue_is_not_media():
    # Non-media tasks must never land on the GPU queue (it would spin up a paid pod).
    assert celery_app.conf.task_default_queue != MEDIA_QUEUE


def test_probe_task_registered():
    assert "media.probe" in celery_app.tasks


def test_acks_late_for_media_tasks():
    # acks_late: a pod killed mid-job (teardown race, spot instance loss) re-delivers the
    # message instead of silently dropping it — retries/visibility is an S5.0 AC.
    assert probe.acks_late is True


def test_retry_policy_matches_worker_max_attempts():
    # max_retries counts *re*-deliveries: first run + retries == MAX_ATTEMPTS, matching the
    # in-process worker loop's contract.
    assert probe.max_retries == MAX_ATTEMPTS - 1


@requires_redis
def test_no_worker_means_offline_and_not_busy():
    # The orchestrator's boot decision keys off these two observations; with no worker
    # process on the broker both must be False (a True here would suppress provisioning).
    from app.modules.media.queue import media_worker_busy, media_worker_online

    assert media_worker_online(timeout=0.5) is False
    assert media_worker_busy(timeout=0.5) is False


@requires_redis
def test_enqueue_increments_media_queue_depth():
    _drain_media_queue()
    probe.delay("depth-check")
    try:
        assert media_queue_depth() == 1
    finally:
        _drain_media_queue()


@requires_redis
def test_cold_start_enqueue_waits_without_worker_then_completes():
    # Cold-start AC (issue #28): a job enqueued with NO worker up must wait on the queue
    # (nothing blocks, nothing is lost) and complete once a worker joins.
    from celery.contrib.testing.worker import start_worker

    _drain_media_queue()
    result = probe.delay("cold-start")
    assert media_queue_depth() == 1  # parked on the broker, no worker yet

    with start_worker(celery_app, queues=[MEDIA_QUEUE], perform_ping_check=False):
        assert result.get(timeout=30) == "cold-start"
    assert media_queue_depth() == 0


def _drain_media_queue() -> None:
    client = redis.Redis.from_url(settings.celery_broker_url)
    client.delete(MEDIA_QUEUE)
