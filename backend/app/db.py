"""SQLModel engine + session on SQLite (WAL).

v1 storage per TECH_SPEC §4: a single SQLite file, WAL journal mode for concurrent
reads alongside the in-process worker loop, and a busy_timeout so writers wait on the
lock instead of erroring. No Alembic in v1 — `init_db()` bootstraps the schema via
`SQLModel.metadata.create_all`. Postgres-ready: models stay vendor-neutral so Phase B
can swap the engine URL.
"""

from collections.abc import Iterator

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

# Additive columns introduced *after* their table was first created — create_all() can't add these
# to a pre-existing SQLite table (it only creates missing tables), so init_db backfills them with a
# guarded ADD COLUMN. Nullable/defaulted only; existing rows take the default.
# ponytail: explicit per-column list, not a generic reconciler — v1 has no migration tooling by
# design (see module docstring). Add a line when a story adds a post-hoc column; reach for Alembic
# only if this list gets long.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("content_item", "spot_check", "BOOLEAN NOT NULL DEFAULT 0"),  # S4.9
)

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


def _backfill_additive_columns(target: Engine) -> None:
    """Add post-hoc columns create_all() can't add to already-existing tables. Idempotent: skips a
    column that already exists (e.g. on a fresh DB create_all just made it)."""
    inspector = inspect(target)
    existing_tables = set(inspector.get_table_names())
    for table, column, ddl in _ADDITIVE_COLUMNS:
        if table not in existing_tables:
            continue  # create_all made the whole table (with the column) — nothing to backfill
        if column in {c["name"] for c in inspector.get_columns(table)}:
            continue
        with target.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def init_db() -> None:
    """Create tables for all imported models, then backfill post-hoc columns. Idempotent."""
    import app.models  # noqa: F401  — register tables on metadata

    SQLModel.metadata.create_all(engine)
    _backfill_additive_columns(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: a session scoped to one request."""
    with Session(engine) as session:
        yield session
