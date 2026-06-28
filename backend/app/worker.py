"""In-process worker loop for `job_run` rows (TECH_SPEC §1/§4).

`run_due_jobs` is a plain synchronous pass over queued rows — deterministic and
trivially testable (no threads, no sleeps). The scheduler just calls it on an interval.
A failing handler increments `attempts` and re-queues until MAX_ATTEMPTS, then fails.

Handlers register by `kind` via `@handler("noop")`. v1 ships only `noop` — real crank
handlers register here in P4.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.models import JobRun, JobStatus

MAX_ATTEMPTS = 3

# kind -> handler. A handler does the work and returns token cost in cents (0 if none).
JobHandler = Callable[[JobRun, Session], int]
_HANDLERS: dict[str, JobHandler] = {}


def handler(kind: str) -> Callable[[JobHandler], JobHandler]:
    def register(fn: JobHandler) -> JobHandler:
        _HANDLERS[kind] = fn
        return fn

    return register


@handler("noop")
def _noop(_job: JobRun, _session: Session) -> int:
    """No-op job used to prove the scheduler → job_run → worker round-trip."""
    return 0


def _utcnow() -> datetime:
    return datetime.now(UTC)


def enqueue(session: Session, kind: str, product_id: int | None = None) -> JobRun:
    """Insert a queued job_run and return it."""
    job = JobRun(kind=kind, product_id=product_id)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def reclaim_running_jobs(session: Session) -> int:
    """Requeue jobs stuck in RUNNING — orphaned by a crash mid-handler.

    v1 runs a single in-process worker, so any RUNNING row at startup is orphaned
    (no live worker owns it). Flip it back to QUEUED for another attempt. Returns the
    count reclaimed. ponytail: a lease/timeout field belongs in Phase B, when multiple
    workers make "stuck vs. in-flight" ambiguous; single-process v1 has no ambiguity.
    """
    stuck = session.exec(select(JobRun).where(JobRun.status == JobStatus.RUNNING)).all()
    for job in stuck:
        job.status = JobStatus.QUEUED
        session.add(job)
    session.commit()
    return len(stuck)


def run_due_jobs(session: Session) -> int:
    """Execute every queued job once. Returns the number of rows processed.

    A handler raising leaves the row queued for a later pass until MAX_ATTEMPTS, at
    which point it is marked FAILED. Unknown kinds fail immediately as configuration
    errors (no point retrying).

    ponytail: assumes a single in-process worker (TECH_SPEC §1). Rows are selected then
    marked RUNNING without an atomic claim — safe because APScheduler runs the worker
    job with max_instances=1 in one process. Phase B (multiple workers) needs a
    SELECT ... FOR UPDATE / atomic UPDATE claim; that arrives with Celery/Postgres.
    """
    jobs = session.exec(select(JobRun).where(JobRun.status == JobStatus.QUEUED)).all()
    for job in jobs:
        job.attempts += 1
        job.status = JobStatus.RUNNING
        job.started_at = _utcnow()
        session.add(job)
        session.commit()

        fn = _HANDLERS.get(job.kind)
        try:
            if fn is None:
                raise LookupError(f"no handler registered for kind={job.kind!r}")
            job.token_cost_cents += fn(job, session)
        except Exception as exc:  # noqa: BLE001 — record any handler failure, never crash the loop
            # Discard any partial writes the handler made (and any broken transaction
            # state) before recording the outcome, so a failed job never commits its
            # side effects. The RUNNING/attempts row was already committed above.
            session.rollback()
            job.error = str(exc)
            unrecoverable = isinstance(exc, LookupError)
            if unrecoverable or job.attempts >= MAX_ATTEMPTS:
                job.status = JobStatus.FAILED
                job.finished_at = _utcnow()
            else:
                job.status = JobStatus.QUEUED  # retry on the next pass
        else:
            job.status = JobStatus.DONE
            job.finished_at = _utcnow()
            job.error = None
        session.add(job)
        session.commit()
    return len(jobs)
