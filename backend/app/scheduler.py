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
    return scheduler
