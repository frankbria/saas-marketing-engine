"""SQLModel engine + session. SQLite (WAL) in v1; Postgres via `SME_DATABASE_URL` (Phase B).

v1 storage per TECH_SPEC §4: a single SQLite file, WAL journal mode for concurrent
reads alongside the in-process worker loop, and a busy_timeout so writers wait on the
lock instead of erroring. No Alembic in v1 — `init_db()` bootstraps the schema via
`SQLModel.metadata.create_all`. S5.0 exercised the Postgres path: `build_engine` keeps
every SQLite-ism (connect_args, PRAGMAs, additive backfill) gated by dialect, so the
URL swap is the whole migration (see infra/POSTGRES_MIGRATION.md for the data copy).
"""

from collections.abc import Iterator

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

# Additive columns introduced *after* their table was first created — create_all() can't add these
# to a pre-existing SQLite table (it only creates missing tables), so init_db backfills them with a
# guarded ADD COLUMN. Nullable/defaulted only; existing rows take the default.
# ponytail: explicit per-column list, not a generic reconciler — v1 has no migration tooling by
# design (see module docstring). Add a line when a story adds a post-hoc column; reach for Alembic
# only if this list gets long. SQLite-only: the DDL strings are SQLite-typed, and a fresh Postgres
# gets every column from create_all (a *pre-existing* Postgres needs Alembic — Phase B+).
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("content_item", "spot_check", "BOOLEAN NOT NULL DEFAULT 0"),  # S4.9
)


def _connect_args(url: str) -> dict:
    # check_same_thread is a sqlite3-only kwarg (the APScheduler worker loop runs in a
    # background thread); psycopg rejects it at connect time.
    return {"check_same_thread": False} if url.startswith("sqlite") else {}


def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Enable WAL + a busy_timeout on every SQLite connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def build_engine(url: str) -> Engine:
    """An engine with the dialect-appropriate connection setup. PRAGMAs are attached only
    to SQLite engines — on Postgres the statements would be syntax errors on connect."""
    eng = create_engine(url, connect_args=_connect_args(url))
    if eng.dialect.name == "sqlite":
        event.listens_for(eng, "connect")(_set_sqlite_pragmas)
    return eng


engine: Engine = build_engine(settings.database_url)


def _backfill_additive_columns(target: Engine) -> None:
    """Add post-hoc columns create_all() can't add to already-existing tables. Idempotent: skips a
    column that already exists (e.g. on a fresh DB create_all just made it). SQLite-only — see
    _ADDITIVE_COLUMNS."""
    if target.dialect.name != "sqlite":
        return
    inspector = inspect(target)
    existing_tables = set(inspector.get_table_names())
    for table, column, ddl in _ADDITIVE_COLUMNS:
        if table not in existing_tables:
            continue  # create_all made the whole table (with the column) — nothing to backfill
        if column in {c["name"] for c in inspector.get_columns(table)}:
            continue
        try:
            with target.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        except OperationalError as exc:
            # Concurrent startup: another process won the check-then-act race and added the column
            # first. That's the outcome we wanted — swallow the duplicate, re-raise anything else.
            if "duplicate column name" not in str(exc).lower():
                raise


def init_db() -> None:
    """Create tables for all imported models, then backfill post-hoc columns. Idempotent."""
    import app.models  # noqa: F401  — register tables on metadata

    SQLModel.metadata.create_all(engine)
    _backfill_additive_columns(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: a session scoped to one request."""
    with Session(engine) as session:
        yield session
