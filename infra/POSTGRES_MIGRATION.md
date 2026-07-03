# SQLite → Postgres migration (Phase B, S5.0)

The app is vendor-neutral by design: models are plain SQLModel, every SQLite-ism in
`backend/app/db.py` (connect_args, WAL PRAGMAs, the additive-column backfill) is gated by
dialect, and the whole engine swap is one env var. CI exercises this path on every push
(`backend/tests/test_postgres_path.py` against a real Postgres service).

## When to migrate

When Celery workers multiply (the in-process worker's single-writer assumption breaks) or
SQLite write contention shows up. Until then SQLite/WAL remains the primary store.

## Procedure

1. **Provision the database** (VPS already runs PostgreSQL 16 on localhost — see
   `infra/deploy/PORTS.md`; for local dev use `infra/compose.dev.yml`, port 5440):

   ```sql
   CREATE USER sme WITH PASSWORD '<from vault>';
   CREATE DATABASE sme OWNER sme;
   ```

2. **Stop the app** (single writer — no live migration needed for a single-operator tool):

   ```bash
   supervisorctl stop sme-backend
   ```

3. **Create the schema** on the empty Postgres by booting the app once against it, or:

   ```bash
   cd backend
   SME_DATABASE_URL='postgresql+psycopg://sme:…@localhost:5432/sme' \
     uv run python -c "from app.db import init_db; init_db()"
   ```

   `create_all` emits every table/column the models declare — on a *fresh* Postgres no
   backfill is needed (the `_ADDITIVE_COLUMNS` mechanism is SQLite-only).

4. **Copy the data** with pgloader (maps SQLite types, preserves rows; booleans stored as
   0/1 convert to true/false):

   ```bash
   pgloader ./sme.db postgresql://sme:…@localhost:5432/sme
   ```

   pgloader creates tables itself by default — since step 3 already made the canonical
   schema, run it with `--with "data only"`:

   ```bash
   pgloader --with "data only" ./sme.db postgresql://sme:…@localhost:5432/sme
   ```

5. **Reset sequences** (pgloader's data-only mode doesn't advance identity sequences —
   without this the first INSERT after migration fails on a duplicate primary key):

   ```sql
   DO $$
   DECLARE r RECORD;
   BEGIN
     FOR r IN SELECT table_name FROM information_schema.columns
              WHERE column_name = 'id' AND table_schema = 'public'
     LOOP
       EXECUTE format(
         'SELECT setval(pg_get_serial_sequence(%L, ''id''), COALESCE((SELECT MAX(id) FROM %I), 1))',
         r.table_name, r.table_name);
     END LOOP;
   END $$;
   ```

6. **Point the app at Postgres and start it**:

   ```bash
   # backend/.env
   SME_DATABASE_URL=postgresql+psycopg://sme:…@localhost:5432/sme
   supervisorctl start sme-backend
   ```

7. **Verify**: `/health` responds; the dashboard shows the same products/content;
   `job_run` rows keep flowing (the heartbeat enqueues a noop every minute).

Keep the SQLite file as the rollback: pointing `SME_DATABASE_URL` back at it restores the
pre-migration state (minus anything written to Postgres in between).

## Known limitations

- **No Alembic in v1.** A column added *after* a Postgres deployment exists cannot ride
  `_ADDITIVE_COLUMNS` (SQLite-only DDL) — that's the point at which Alembic gets adopted
  (`backend/app/db.py` module docstring).
- pgloader is the recommended copier; a hand-rolled `.dump`/CSV path also works for the
  small v1 data volumes but gets the boolean/timestamp conversions wrong more easily.
