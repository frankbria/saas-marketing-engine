"""SQLModel engine + session on SQLite (WAL).

v1 storage per TECH_SPEC §4: a single SQLite file, WAL journal mode for concurrent
reads alongside the in-process worker loop, and a busy_timeout so writers wait on the
lock instead of erroring. No Alembic in v1 — `init_db()` bootstraps the schema via
`SQLModel.metadata.create_all`. Postgres-ready: models stay vendor-neutral so Phase B
can swap the engine URL.
"""

from collections.abc import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

# check_same_thread=False: the APScheduler worker loop runs in a background thread.
engine: Engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Enable WAL + a busy_timeout on every SQLite connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_db() -> None:
    """Create tables for all imported models. Idempotent."""
    import app.models  # noqa: F401  — register tables on metadata

    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: a session scoped to one request."""
    with Session(engine) as session:
        yield session
