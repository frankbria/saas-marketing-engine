"""S0.2: SQLite(WAL) + job_run worker loop + retries.

Tests drive `run_due_jobs` directly against an in-memory-ish temp DB — deterministic,
no scheduler threads, no sleeps. The APScheduler wiring itself is exercised by
test_scheduler_builds (it builds the jobs without starting a background thread).
"""

import tomllib
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app import worker
from app.models import JobRun, JobStatus
from app.scheduler import create_scheduler
from app.worker import (
    MAX_ATTEMPTS,
    enqueue,
    handler,
    reclaim_running_jobs,
    run_due_jobs,
)


@pytest.fixture
def session(tmp_path):
    """A real SQLite file (so WAL is meaningful) with the schema bootstrapped."""
    db = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_wal_enabled(session):
    mode = session.exec(select(JobRun)).all()  # force a connection
    assert mode == []
    journal = session.connection().exec_driver_sql("PRAGMA journal_mode").scalar()
    assert journal.lower() == "wal"


def test_noop_job_round_trips(session):
    job = enqueue(session, "noop")
    assert job.status == JobStatus.QUEUED
    assert job.attempts == 0

    processed = run_due_jobs(session)

    assert processed == 1
    session.refresh(job)
    assert job.status == JobStatus.DONE
    assert job.attempts == 1
    assert job.started_at is not None
    assert job.finished_at is not None
    assert job.error is None


def test_idle_loop_processes_nothing(session):
    assert run_due_jobs(session) == 0


def test_failing_job_retries_then_fails(session):
    calls = {"n": 0}

    @handler("always_fails")
    def _boom(_job, _session):
        calls["n"] += 1
        raise RuntimeError("kaboom")

    try:
        job = enqueue(session, "always_fails")
        # Each pass retries while attempts < MAX_ATTEMPTS, then marks FAILED.
        for _ in range(MAX_ATTEMPTS):
            run_due_jobs(session)
        session.refresh(job)

        assert job.attempts == MAX_ATTEMPTS
        assert calls["n"] == MAX_ATTEMPTS
        assert job.status == JobStatus.FAILED
        assert "kaboom" in job.error
        # Exhausted job is no longer queued, so further passes ignore it.
        assert run_due_jobs(session) == 0
    finally:
        worker._HANDLERS.pop("always_fails", None)


def test_transient_failure_then_success(session):
    attempts_before_success = 1

    @handler("flaky")
    def _flaky(job, _session):
        if job.attempts <= attempts_before_success:
            raise RuntimeError("transient")
        return 7  # token cost

    try:
        job = enqueue(session, "flaky")
        run_due_jobs(session)  # attempt 1 -> fails, re-queued
        session.refresh(job)
        assert job.status == JobStatus.QUEUED

        run_due_jobs(session)  # attempt 2 -> succeeds
        session.refresh(job)
        assert job.status == JobStatus.DONE
        assert job.attempts == 2
        assert job.token_cost_cents == 7
    finally:
        worker._HANDLERS.pop("flaky", None)


def test_failed_handler_partial_writes_are_rolled_back(session):
    """A handler that writes then raises must not commit its side effects."""

    @handler("partial_then_fail")
    def _partial(_job, sess):
        sess.add(JobRun(kind="orphan_side_effect"))  # pending, uncommitted write...
        raise RuntimeError("after the write")  # ...then fail before commit

    try:
        job = enqueue(session, "partial_then_fail")
        run_due_jobs(session)
        session.refresh(job)
        assert job.status == JobStatus.QUEUED  # re-queued (attempt 1 of 3)
        # Without the rollback, recording the failure would flush the orphan add.
        kinds = [j.kind for j in session.exec(select(JobRun)).all()]
        assert kinds == ["partial_then_fail"]
    finally:
        worker._HANDLERS.pop("partial_then_fail", None)


def test_reclaim_running_jobs_requeues_orphans(session):
    job = enqueue(session, "noop")
    job.status = JobStatus.RUNNING  # simulate a crash mid-handler
    session.add(job)
    session.commit()

    reclaimed = reclaim_running_jobs(session)

    assert reclaimed == 1
    session.refresh(job)
    assert job.status == JobStatus.QUEUED
    # And it now runs to completion on the next pass.
    run_due_jobs(session)
    session.refresh(job)
    assert job.status == JobStatus.DONE


def test_unknown_kind_fails_immediately(session):
    job = enqueue(session, "no_such_handler")
    run_due_jobs(session)
    session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.attempts == 1  # no point retrying a config error


def test_scheduler_builds_worker_and_heartbeat_jobs():
    # Built but not started — no background thread to tear down.
    scheduler = create_scheduler()
    ids = {j.id for j in scheduler.get_jobs()}
    assert ids == {"worker", "heartbeat"}


def test_no_queue_cluster_deps_in_v1():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    deps = " ".join(data["project"]["dependencies"]).lower()
    for banned in ("celery", "redis", "psycopg", "postgres"):
        assert banned not in deps, f"{banned} must be Phase B, not v1"
