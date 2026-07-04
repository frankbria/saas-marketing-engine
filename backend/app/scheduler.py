"""APScheduler setup + the worker-loop tick (TECH_SPEC §1).

In-process BackgroundScheduler, no queue cluster (Celery/Redis is Phase B). Two interval
jobs: a `heartbeat` that enqueues a noop job_run (proving the scheduler path), and a
`worker` tick that drains the queue via the in-process worker loop. The seam is clean —
Phase B swaps these for Celery beat + workers without touching callers.
"""

from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.config import settings
from app.db import engine
from app.modules.crank.crank import enqueue_due_cranks
from app.modules.crank.publish import pace_content, publish_scheduled
from app.modules.crank.video_pipeline import advance_video_renders
from app.modules.heartbeat import run_heartbeat
from app.modules.media.orchestrator import run_provisioner_tick
from app.worker import enqueue, run_due_jobs


def _heartbeat() -> None:
    with Session(engine) as session:
        enqueue(session, "noop")


def _worker_tick() -> None:
    with Session(engine) as session:
        run_due_jobs(session)


def _crank_tick() -> None:
    with Session(engine) as session:
        enqueue_due_cranks(session, datetime.now(UTC))


def _heartbeat_digest_tick() -> None:
    # S6.2 daily digest + alerts (§8.4). Polled hourly with an hour-of-day guard instead of a
    # cron trigger: a process that was down at the digest hour still catches up on its next tick
    # (the watchdog must not silently skip a day), and run_heartbeat's per-UTC-day idempotency
    # makes every extra tick a no-op.
    now = datetime.now(UTC)
    if now.hour < settings.heartbeat_digest_hour_utc:
        return
    with Session(engine) as session:
        run_heartbeat(session, now)


def _publish_tick() -> None:
    # Pace newly-vetted items, then publish everything now due (S4.5). Same cadence-check interval
    # as the crank — the per-channel `daily_cap` does the real pacing, not the poll granularity.
    with Session(engine) as session:
        now = datetime.now(UTC)
        pace_content(session, now)
        publish_scheduled(session, now)


def _media_provisioner_tick() -> None:
    # S5.0: ephemeral-GPU boot/teardown decision (issue #28). The tick's cold path (empty
    # queue, no lease) touches neither Redis-provider nor API key, so it is safe to run
    # everywhere; run_provisioner_tick never raises.
    with Session(engine) as session:
        run_provisioner_tick(session, datetime.now(UTC))


def _video_render_tick() -> None:
    # S5.1: dispatch/collect GPU renders for `rendering` items. advance_video_renders never
    # raises, and its cold path (no rendering items) touches neither the broker nor the workspace.
    with Session(engine) as session:
        advance_video_renders(session, datetime.now(UTC))


def create_scheduler() -> BackgroundScheduler:
    """Build (but don't start) the scheduler with the v1 interval jobs."""
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _worker_tick, "interval", seconds=settings.worker_interval_seconds, id="worker"
    )
    scheduler.add_job(
        _heartbeat, "interval", seconds=settings.heartbeat_interval_seconds, id="heartbeat"
    )
    scheduler.add_job(
        _crank_tick, "interval", seconds=settings.crank_check_interval_seconds, id="crank"
    )
    scheduler.add_job(
        _publish_tick, "interval", seconds=settings.crank_check_interval_seconds, id="publish"
    )
    scheduler.add_job(
        _heartbeat_digest_tick,
        "interval",
        seconds=settings.heartbeat_digest_check_interval_seconds,
        id="heartbeat_digest",
    )
    scheduler.add_job(
        _media_provisioner_tick,
        "interval",
        seconds=settings.media_provisioner_interval_seconds,
        id="media_provisioner",
    )
    scheduler.add_job(
        _video_render_tick,
        "interval",
        seconds=settings.video_render_tick_seconds,
        id="video_render",
    )
    return scheduler
