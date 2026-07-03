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
from app.modules.heartbeat import run_heartbeat
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
    return scheduler
