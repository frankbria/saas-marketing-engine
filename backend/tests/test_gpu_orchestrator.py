"""S5.0: ephemeral-GPU orchestration loop + spend guardrails (issue #28).

The state machine is exercised with a hand-written fake provider and injected
depth/busy/online observations (the spot-check `sample=` pattern), a real SQLite DB, and
a fixed NOW — no sleeps, no mocking of infrastructure. The cold-start end-to-end test at
the bottom runs the real Redis + Celery worker path with only the provider faked.
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import settings
from app.models import GpuLease, GpuLeaseStatus
from app.modules.media import orchestrator
from app.modules.media.orchestrator import (
    month_to_date_gpu_cost_cents,
    run_provisioner_tick,
)
from app.modules.media.provisioner import PodState

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(eng, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture(autouse=True)
def _reset_alert_edges():
    orchestrator._alerted.clear()
    yield
    orchestrator._alerted.clear()


class _FakeGpuProvider:
    """Records calls; scriptable teardown verification."""

    def __init__(self, verify_teardown=True):
        self.ensure_calls = 0
        self.teardown_calls: list[str] = []
        self.verify_teardown = verify_teardown
        self.pod_alive = False
        self.fail_ensure = False

    def ensure_worker(self) -> str:
        self.ensure_calls += 1
        if self.fail_ensure:
            raise RuntimeError("provider capacity exhausted")
        self.pod_alive = True
        return f"pod-{self.ensure_calls}"

    def status(self) -> PodState:
        return PodState.RUNNING if self.pod_alive else PodState.NONE

    def teardown(self, pod_id: str) -> bool:
        self.teardown_calls.append(pod_id)
        if self.verify_teardown:
            self.pod_alive = False
        return self.verify_teardown


def _tick(session, provider, *, now=NOW, depth=0, online=False, busy=False):
    run_provisioner_tick(
        session,
        now,
        provider=provider,
        queue_depth=lambda: depth,
        worker_online=lambda: online,
        worker_busy=lambda: busy,
    )


def _active_lease(session) -> GpuLease | None:
    return session.exec(select(GpuLease).where(GpuLease.status == GpuLeaseStatus.ACTIVE)).first()


def _utc(dt: datetime) -> datetime:
    # SQLite hands datetimes back tz-naive; normalize before comparing to the aware NOW.
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def test_boots_worker_when_jobs_pending_and_no_worker(session):
    provider = _FakeGpuProvider()
    _tick(session, provider, depth=3)
    assert provider.ensure_calls == 1
    lease = _active_lease(session)
    assert lease is not None
    assert lease.pod_id == "pod-1"
    assert lease.provider == settings.gpu_provider
    assert _utc(lease.started_at) == NOW


def test_does_not_double_boot_while_lease_active(session):
    provider = _FakeGpuProvider()
    _tick(session, provider, depth=3)
    _tick(session, provider, depth=3)  # pod still booting: no worker online yet
    assert provider.ensure_calls == 1


def test_no_provider_touched_when_queue_empty_and_no_lease(session, monkeypatch):
    # The 60s tick's cold path must not build a provider (no API key needed in dev).
    def _explode():
        raise AssertionError("provider must not be built on the idle path")

    monkeypatch.setattr("app.modules.media.provisioner.build_provider", _explode)
    run_provisioner_tick(session, NOW, queue_depth=lambda: 0)
    assert _active_lease(session) is None


def test_idle_is_marked_but_not_torn_down_before_threshold(session):
    provider = _FakeGpuProvider()
    _tick(session, provider, depth=1)
    _tick(session, provider, depth=0, online=True)
    lease = _active_lease(session)
    assert _utc(lease.idle_since) == NOW
    assert provider.teardown_calls == []


def test_activity_clears_idle_marker(session):
    provider = _FakeGpuProvider()
    _tick(session, provider, depth=1)
    _tick(session, provider, depth=0, online=True)
    _tick(session, provider, depth=2, online=True)  # new work arrived
    assert _active_lease(session).idle_since is None


def test_busy_worker_is_not_idle_even_at_zero_depth(session):
    # acks_late + prefetch 1: an in-flight job's message is delivered (LLEN 0) but unacked.
    # Depth alone would tear the pod down mid-job — the busy check prevents that.
    provider = _FakeGpuProvider()
    _tick(session, provider, depth=1)
    _tick(session, provider, depth=0, online=True, busy=True)
    assert _active_lease(session).idle_since is None
    assert provider.teardown_calls == []


def test_idle_past_threshold_tears_down_and_closes_lease(session):
    provider = _FakeGpuProvider()
    _tick(session, provider, depth=1)
    _tick(session, provider, depth=0, online=True)
    later = NOW + timedelta(minutes=settings.gpu_idle_teardown_minutes + 1)
    _tick(session, provider, now=later, depth=0, online=True)

    assert provider.teardown_calls == ["pod-1"]
    lease = session.exec(select(GpuLease)).one()
    assert lease.status == GpuLeaseStatus.ENDED
    assert _utc(lease.ended_at) == later
    # Lease ran NOW → NOW+11min: 11 minutes at the configured per-minute rate.
    assert lease.cost_cents == 11 * settings.gpu_pod_rate_cents_per_minute


def test_unverified_teardown_alerts_and_flags_lease(session, caplog):
    provider = _FakeGpuProvider(verify_teardown=False)
    _tick(session, provider, depth=1)
    _tick(session, provider, depth=0, online=True)
    later = NOW + timedelta(minutes=settings.gpu_idle_teardown_minutes + 1)
    with caplog.at_level(logging.WARNING):
        _tick(session, provider, now=later, depth=0, online=True)

    lease = session.exec(select(GpuLease)).one()
    assert lease.status == GpuLeaseStatus.TEARDOWN_UNVERIFIED
    assert any("gpu_teardown_unverified" in r.message for r in caplog.records)


def test_cap_refuses_provisioning_and_alerts(session, monkeypatch, caplog):
    monkeypatch.setattr(settings, "media_gpu_monthly_cap_cents", 100)
    session.add(
        GpuLease(
            provider="runpod",
            pod_id="pod-old",
            status=GpuLeaseStatus.ENDED,
            started_at=NOW - timedelta(days=2),
            ended_at=NOW - timedelta(days=2, minutes=-50),
            cost_cents=100,
        )
    )
    session.commit()

    provider = _FakeGpuProvider()
    with caplog.at_level(logging.WARNING):
        _tick(session, provider, depth=5)

    assert provider.ensure_calls == 0  # jobs wait on the queue; nothing is lost
    assert _active_lease(session) is None
    assert any("gpu_spend_cap" in r.message for r in caplog.records)


def test_cap_alert_fires_once_not_every_tick(session, monkeypatch, caplog):
    monkeypatch.setattr(settings, "media_gpu_monthly_cap_cents", 100)
    session.add(
        GpuLease(
            provider="runpod",
            pod_id="pod-old",
            status=GpuLeaseStatus.ENDED,
            started_at=NOW - timedelta(days=2),
            cost_cents=100,
        )
    )
    session.commit()
    provider = _FakeGpuProvider()
    with caplog.at_level(logging.WARNING):
        _tick(session, provider, depth=5)
        _tick(session, provider, depth=5)
    assert sum("gpu_spend_cap" in r.message for r in caplog.records) == 1


def test_under_cap_still_boots(session, monkeypatch):
    monkeypatch.setattr(settings, "media_gpu_monthly_cap_cents", 10_000)
    provider = _FakeGpuProvider()
    _tick(session, provider, depth=1)
    assert provider.ensure_calls == 1


def test_month_to_date_includes_active_lease_accrual(session):
    session.add(
        GpuLease(
            provider="runpod",
            pod_id="pod-live",
            status=GpuLeaseStatus.ACTIVE,
            started_at=NOW - timedelta(minutes=30),
        )
    )
    session.commit()
    expected = 30 * settings.gpu_pod_rate_cents_per_minute
    assert month_to_date_gpu_cost_cents(session, NOW) == expected


def test_month_to_date_ignores_previous_months(session):
    session.add(
        GpuLease(
            provider="runpod",
            pod_id="pod-june",
            status=GpuLeaseStatus.ENDED,
            started_at=NOW - timedelta(days=40),
            cost_cents=9_999,
        )
    )
    session.commit()
    assert month_to_date_gpu_cost_cents(session, NOW) == 0


def test_tick_never_raises_on_provider_failure(session, caplog):
    provider = _FakeGpuProvider()
    provider.fail_ensure = True
    with caplog.at_level(logging.WARNING):
        _tick(session, provider, depth=1)  # must not raise out of the scheduler tick
    assert any("gpu_provision_failed" in r.message for r in caplog.records)
    assert _active_lease(session) is None


# --- cold-start end-to-end (real Redis + real Celery worker; provider faked) ----------

import redis  # noqa: E402

from app.celery_app import MEDIA_QUEUE, celery_app  # noqa: E402
from app.modules.media.queue import media_queue_depth  # noqa: E402
from app.modules.media.tasks import probe  # noqa: E402


def _redis_available() -> bool:
    try:
        redis.Redis.from_url(settings.celery_broker_url, socket_connect_timeout=1).ping()
        return True
    except (redis.exceptions.RedisError, OSError):
        return False


@pytest.mark.skipif(not _redis_available(), reason="requires Redis at SME_CELERY_BROKER_URL")
def test_cold_start_end_to_end(session):
    """Issue #28 AC: a job enqueued with no worker up completes without manual action —
    provisioner boots (fake provider), job runs, queue drains, idle teardown fires."""
    from celery.contrib.testing.worker import start_worker

    redis.Redis.from_url(settings.celery_broker_url).delete(MEDIA_QUEUE)
    provider = _FakeGpuProvider()

    result = probe.delay("cold-start-e2e")  # no worker exists yet; the job just waits

    run_provisioner_tick(
        session, NOW, provider=provider, queue_depth=media_queue_depth,
        worker_online=lambda: False, worker_busy=lambda: False,
    )  # fmt: skip
    assert provider.ensure_calls == 1  # pending work + no worker → pod boots
    assert _active_lease(session) is not None

    # The "pod" joins the queue (a real worker on the real broker) and drains the job.
    with start_worker(celery_app, queues=[MEDIA_QUEUE], perform_ping_check=False):
        assert result.get(timeout=30) == "cold-start-e2e"

    # Queue idle → mark, then cross the threshold → verified teardown, lease closed.
    t1 = NOW + timedelta(minutes=1)
    run_provisioner_tick(
        session, t1, provider=provider, queue_depth=media_queue_depth,
        worker_online=lambda: True, worker_busy=lambda: False,
    )  # fmt: skip
    t2 = t1 + timedelta(minutes=settings.gpu_idle_teardown_minutes + 1)
    run_provisioner_tick(
        session, t2, provider=provider, queue_depth=media_queue_depth,
        worker_online=lambda: True, worker_busy=lambda: False,
    )  # fmt: skip

    assert provider.teardown_calls == ["pod-1"]
    assert provider.pod_alive is False  # destroyed at the provider = billing stopped
    lease = session.exec(select(GpuLease)).one()
    assert lease.status == GpuLeaseStatus.ENDED
    assert lease.cost_cents > 0
