"""S4.9 regression: init_db backfills post-hoc columns on a pre-existing SQLite DB.

`create_all()` only creates missing *tables* — it can't add a new column to a table that already
exists. `_backfill_additive_columns` closes that gap so pulling S4.9 onto an older dev/deployed DB
doesn't fail with `no such column: content_item.spot_check`.
"""

from sqlalchemy import create_engine, inspect, text

from app.db import _backfill_additive_columns


def _columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_backfill_adds_missing_column_to_existing_table(tmp_path):
    db = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db}")
    # An "old" DB: content_item exists but predates the spot_check column.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE content_item (id INTEGER PRIMARY KEY, body TEXT)"))
        conn.execute(text("INSERT INTO content_item (body) VALUES ('pre-existing row')"))

    assert "spot_check" not in _columns(engine, "content_item")

    _backfill_additive_columns(engine)

    assert "spot_check" in _columns(engine, "content_item")
    # Existing row gets the NOT NULL default (0), so reads/inserts don't crash.
    with engine.begin() as conn:
        assert conn.execute(text("SELECT spot_check FROM content_item")).scalar() == 0


def test_backfill_is_idempotent_and_noop_when_column_present(tmp_path):
    db = tmp_path / "cur.db"
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE content_item (id INTEGER PRIMARY KEY, spot_check BOOLEAN DEFAULT 0)")
        )

    _backfill_additive_columns(engine)  # column already there → no-op, no error
    _backfill_additive_columns(engine)  # second run stays a no-op

    assert "spot_check" in _columns(engine, "content_item")


def test_backfill_skips_when_table_absent(tmp_path):
    db = tmp_path / "empty.db"
    engine = create_engine(f"sqlite:///{db}")
    _backfill_additive_columns(engine)  # no content_item table yet → create_all's job, not ours
    assert "content_item" not in inspect(engine).get_table_names()
