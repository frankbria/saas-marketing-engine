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
**Corollary (S4.2):** because there's no ALTER path, pre-seed a new table's forward-looking columns
*and their constraints* (e.g. `idempotency_key` UNIQUE) at `create_all` time, not just the columns —
adding the constraint later would need the very ALTER we're avoiding.

## Run `black`/`ruff` from `backend/`, never the repo root.
The backend's line-length + tool config lives in `backend/pyproject.toml`. Running `uv run black .` from
the repo root finds no config → reformats with black's default line-length 88, silently mangling 50+
already-clean files (and `ruff`/`pytest` fail to spawn there — no venv). In S4.2 this rewrote the whole
backend before I caught it. Recovery: `git checkout -- backend/` restores the (correctly-formatted)
staged snapshot; re-apply post-stage edits and re-run tools **with `cd backend` first**.
**How to apply:** always `cd /…/backend` before any `uv run` command; verify the working dir when a
formatter reports reformatting files you didn't touch.

## Validate cheap/structural preconditions before external-dependency setup.
S4.2's `_real_generate` built the Anthropic client (needs `SME_ANTHROPIC_API_KEY`) *before* checking the
content_type, so the unsupported-type `LookupError` guard couldn't fire without a key — it passed locally
(key present) but failed CI (no key). Cheap validation that gates on inputs must run before any call that
needs a secret/network, so the guard behaves identically in every environment.
**How to apply:** order a handler as: validate identity/inputs → check budget/preconditions → only then
build clients / hit the network. Reproduce key-gated paths locally with `env -u SME_… uv run pytest …`.
