# Lessons

## Deterministic number-matching needs BOTH type- and format-scoping (S4.4 guard).
S4.4's claim-trace checks that a claimed number appears in the fact corpus. Two bugs, each caught by a
different review bot, not by my first tests:
- **Cross-type (codex):** a flat "number appears anywhere in facts" set let a `$99` price vouch for a
  `99%` or `99x` claim. Fix: bucket facts by claim *kind* (percent/multiplier/money/count) and trace
  each claim only to its own bucket.
- **Format (CodeRabbit):** money facts built from `cents // 100` only held whole dollars, so a real
  `$19.99` price false-blocked its own `$19.99` claim. Fix: add every legit representation
  (cents `1999`, dollars `19`, dollars.cents `19.99`) to the money bucket.
**How to apply:** when a deterministic guard matches extracted tokens against an allowed set, ask
"could the *same token* mean different things?" (type-scope) and "does my source emit *every*
surface form the content might use?" (format-scope). Add a regression test per failure mode. Numeric
safety heuristics err toward false-blocks fast — run the diff past a cross-family review before merge.

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

## `gh pr checks --watch` can report a stale run — verify against the head SHA before merging.
In S4.3 a docs-only push triggered a fresh CI run, but `--watch` returned green immediately from the
*previous* commit's already-finished checks (a race before the new run registered). Merging then would
have skipped the head commit's checks. Confirm the run you watched belongs to the current head:
`gh pr view <N> --json headRefOid,mergeStateStatus` — `mergeStateStatus` should be `CLEAN`, not
`UNSTABLE`/`BLOCKING`, and `gh pr checks <N>` should show no `pending` for the head SHA.
**How to apply:** after any push, re-check `mergeStateStatus == CLEAN` (or explicitly watch again) before
`gh pr merge`; treat a lone green `--watch` as necessary-but-not-sufficient.

## `gh pr edit --body` fails on this repo (Projects-classic GraphQL error) — PATCH via REST instead.
`gh pr edit 58 --body-file …` exits non-zero with "Projects (classic) is being deprecated …
(repository.pullRequest.projectCards)" because the edit mutation still queries projectCards. Editing a
PR body/title through `gh pr edit` will keep failing here.
**How to apply:** update the body with the REST endpoint, which doesn't touch projectCards:
`gh api repos/<owner>/<repo>/pulls/<N> -X PATCH -F body=@body.md`.

## The generate handler now owns the critic gate (S4.3) — a costly, non-idempotent job.
`modules/crank/generate.py`'s `run_generate` loops generate→critique up to `1+critic_max_regenerations`,
so one `generate` job can make several paired opus+haiku calls. Any story that touches this handler
must keep: the per-attempt budget reservation (`_reserve_one_attempt`), the one-`ContentItem`-per-cell
invariant (novelty's `_TERMINAL_FAILURE` exclusion depends on it), and the different critic tier
(`CRITIC_MODEL` haiku ≠ `GEN_MODEL` opus, FR-22). Partial-spend-on-retry is the shared worker limitation
(no cost ledger) — documented, not fixed per-story.

## Publish adapters (S4.5): idempotency + transient/permanent errors on external side-effects.
`app/channels/` adapters (`BlogAdapter`, `RedditAdapter`) publish a vetted `content_item` and must be
idempotent + retry-safe. Patterns the cross-family + CodeRabbit reviews enforced (apply to any future
adapter, e.g. X/IG/YouTube in Phase B):
- **Idempotency keyed on `idempotency_key`, not incidental fields.** Reddit has no native idempotency
  key, so each post embeds a `^(sme-ref:<idempotency_key>)` footer and the pre-submit remote scan
  matches the **full self-delimiting footer** — a bare-substring match on `sme-ref:reddit:7` collides
  with `...:70`, and a title-based match collides across two items that share a headline. Blog is
  idempotent by construction (write to `<slug>-<item.id>.html` atomically via temp file + `os.replace`;
  slug alone collides across items).
- **Split transient vs permanent errors.** Retry only network/`prawcore` (RequestException/ServerError/
  TooManyRequests) + a `RedditAPIException` whose items include `RATELIMIT`; let validation/auth errors
  surface so `publish_scheduled` records `publish_failed` instead of retrying a doomed post forever. A
  blanket `except Exception → Retryable` is an infinite-retry bug.
- **Re-check kill-switch state at publish time**, not just when pacing schedules the item — `paused`,
  `enabled`, AND `autonomous` can all flip after scheduling; `publish_scheduled` re-checks all three.
- **Fail closed on a missing required key.** No `idempotency_key` ⇒ raise (permanent), don't silently
  publish without the idempotency guard.
**How to apply:** publish path = validate inputs/key (fail-closed, permanent) → check remote by the
durable key → submit → classify errors transient-vs-permanent. Inject the network client (like
`generate=`/`critique=`) so the worker path is testable without a real account.
