"""ORM models (TECH_SPEC §4). Import each so it registers on SQLModel.metadata."""

from app.models.job_run import JobRun, JobStatus

__all__ = ["JobRun", "JobStatus"]
