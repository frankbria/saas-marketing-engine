# Lessons

## Moving a state transition between endpoints? Check the frontend callers.
S2.8 moved the `setup_done → qa` crossing out of the S2.7 smoke-test endpoint into the new
launch-checklist endpoint (spec-correct). The backend + its tests were self-consistent, but the
dashboard's `smoke-test.tsx` still assumed "smoke pass → qa" and had no UI to call the new endpoint —
an operator would get a passing smoke test and be stranded in `setup_done`. Caught by the `codex`
cross-family review, not by tests.
**How to apply:** when you change *which* endpoint owns a lifecycle transition, grep the other
surface (dashboard/CLI) for callers of the old endpoint and update the flow + copy in the same PR.

## v1 has no DB migrations — new columns need a documented recreate path.
`db.py` uses `create_all` only ("No Alembic in v1"). Adding a column to `Product` (like `smoke_test_json`
in S2.7, `launch_checklist_json` in S2.8) does NOT alter an existing SQLite file — the ORM then selects
a column the table lacks. This is an accepted v1 tradeoff (no live DB yet; recreate dev DB), not a per-PR
bug to fix with Alembic. Note it as a Known Limitation rather than introducing migration machinery.
