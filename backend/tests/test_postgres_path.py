"""S5.0: SQLite → Postgres migration path exercised (issue #28).

The unit tests pin the dialect-dependent engine construction (connect_args, PRAGMA
listener, additive-column backfill gating). The integration tests boot the real schema
on a real Postgres and run a job round-trip — no mocks. They need a server:

    docker compose -f infra/compose.dev.yml up -d postgres
    export SME_TEST_POSTGRES_URL=postgresql+psycopg://sme:sme@localhost:5440/sme

CI provides one via a services block, so the path is exercised on every push.
"""

import os

import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel

from app.db import _backfill_additive_columns, _connect_args, build_engine
from app.models import JobStatus
from app.worker import enqueue, run_due_jobs

POSTGRES_URL = os.environ.get("SME_TEST_POSTGRES_URL")

requires_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set SME_TEST_POSTGRES_URL (see infra/compose.dev.yml) to run Postgres-path tests",
)


# --- dialect-dependent construction (no server needed) --------------------------------


def test_connect_args_sqlite_only():
    # check_same_thread is a sqlite3-only kwarg; psycopg would reject it at connect time.
    assert _connect_args("sqlite:///./sme.db") == {"check_same_thread": False}
    assert _connect_args("postgresql+psycopg://u:p@h/db") == {}


def test_sqlite_engine_still_gets_wal_pragmas(tmp_path):
    eng = build_engine(f"sqlite:///{tmp_path / 'wal.db'}")
    with eng.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


# --- the real thing --------------------------------------------------------------------


@pytest.fixture
def pg_engine():
    eng = build_engine(POSTGRES_URL)
    SQLModel.metadata.drop_all(eng)
    SQLModel.metadata.create_all(eng)
    yield eng
    SQLModel.metadata.drop_all(eng)
    eng.dispose()


@requires_postgres
def test_schema_boots_on_postgres(pg_engine):
    # create_all is the migration path for a fresh Postgres (no Alembic in v1) — every
    # table the app knows must materialize, including S5.0's gpu_lease.
    tables = set(inspect(pg_engine).get_table_names())
    for expected in ("product", "job_run", "content_item", "credential", "gpu_lease"):
        assert expected in tables


@requires_postgres
def test_connecting_applies_no_sqlite_pragmas(pg_engine):
    # If the PRAGMA listener leaked onto a Postgres engine, the first connect would die
    # with a syntax error — a plain round-trip proves the guard.
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


@requires_postgres
def test_additive_backfill_is_gated_off_postgres(pg_engine):
    # The backfill DDL strings are SQLite-typed (BOOLEAN ... DEFAULT 0); on Postgres a
    # fresh create_all already made every column, so the backfill must no-op, not run DDL.
    _backfill_additive_columns(pg_engine)  # must not raise
    cols = {c["name"] for c in inspect(pg_engine).get_columns("content_item")}
    assert "spot_check" in cols  # came from create_all, not the backfill


@requires_postgres
def test_job_round_trip_on_postgres(pg_engine):
    # The same enqueue → run_due_jobs flow the app runs on SQLite, on Postgres.
    with Session(pg_engine) as session:
        job = enqueue(session, "noop")
        assert job.status == JobStatus.QUEUED
        run_due_jobs(session)
        session.refresh(job)
        assert job.status == JobStatus.DONE
