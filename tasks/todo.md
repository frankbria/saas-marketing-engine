# S4.5 — Publish adapters: blog + Reddit (idempotent + paced) · issue #23

API-first publishing (TECH_SPEC §7, §8.2/§8.3, PRD FR-21/FR-25, USER_STORIES S4.5).
Owned blog (zero ToS risk) + Reddit (PRAW). No browser fallback; IG/X/YouTube deferred.
Continues the pipeline: `critic_passed`/`guard`-clean → **pace/schedule → publish → record metrics**.

## Acceptance criteria (from issue)
- [ ] `ChannelAdapter` Protocol (`publish`, `delete`); blog (file write) + Reddit (PRAW) adapters
- [ ] Idempotent on `idempotency_key` (check remote before re-post)
- [ ] Pacing: `scheduled_for` spread across cadence window + per-channel `daily_cap`
- [ ] Results + per-item metrics recorded; transient failures retry
- [ ] Reddit: value-first/non-promo policy + per-subreddit rules respected

## Design (self-authored plan — no architectural fork)

Two deterministic periodic passes (mirror `enqueue_due_cranks`: pure, `now` injected, one
per-item state transition), plus adapters in the pre-existing empty `app/channels/` package
(TECH_SPEC §88 puts publishing adapters there).

### 1. `app/channels/base.py` — adapter contract
- `PublishResult` dataclass (`external_url: str`).
- `Retryable(Exception)` — raised by adapters for transient failures (network/rate-limit).
- `ChannelAdapter` Protocol: `publish(item, creds) -> PublishResult`, `delete(external_url, creds) -> None`.
- `get_adapter(channel_type) -> ChannelAdapter` registry (blog, reddit).

### 2. `app/channels/blog.py` — `BlogAdapter` (owned site, zero ToS risk)
- `publish`: render item → HTML file under `workspace_path(slug)/site/blog/<post-slug>.html`
  (post-slug from `meta_json.slug`, else item id). Returns `external_url =
  https://{marketing_domain}/blog/<post-slug>` (falls back to `public_api_base_url` if no domain).
  **Idempotent**: writing the same path is overwrite-safe; existence *is* the remote check.
- `delete`: remove the file (feeds S4.7 retract). No-op if already gone.
- Pure filesystem (no network) → tested directly, no injection needed.

### 3. `app/channels/reddit.py` — `RedditAdapter` (PRAW)
- `publish`: lazy-`import praw` inside the method (module imports without praw; keeps the stub
  path network-free), build a client from decrypted `reddit_oauth` creds, submit a self/text post
  to the channel's target subreddit (+ optional flair) read from `channel.profile_json`; return the
  permalink. Wrap PRAW/network errors in `Retryable`. Per-subreddit rules (subreddit, flair)
  honored from `profile_json`; value-first/non-promo content is enforced **upstream** (critic S4.3 +
  guard S4.4) — the adapter carries already-vetted copy.
- `delete`: PRAW `submission.delete()` for retract.
- The PRAW client factory is injectable so the mapping (item→submission, permalink extraction) is
  tested with a fake submitter, no network — same seam style as `generate=`/`critique=`.
- Add `praw` to `pyproject.toml` dependencies (spec-named lib).

### 4. `app/modules/crank/publish.py` — pace + publish passes
- `pace_content(session, now)`: for each enabled, autonomous, non-paused channel, take its
  `CRITIC_PASSED` items (oldest first) and assign `scheduled_for` + `idempotency_key`
  (`f"{channel.type}:{item.id}"`), status → `SCHEDULED`.
  **Pacing rule** (deterministic, satisfies both "spread across window" + "daily_cap"): step
  successive items by `interval = window / daily_cap` (window = product cadence, default weekly),
  starting at `max(now, last_scheduled_for_channel + interval)`. `interval` ≥ 1 day guarantees ≤
  `daily_cap` land in any 24 h and spreads them across the cadence window. `daily_cap` unset →
  spread the batch evenly across the window (`interval = window / batch_size`).
- `publish_scheduled(session, now, *, adapter_for=get_adapter)`: for each `SCHEDULED` item with
  `scheduled_for <= now`, re-check `channel.paused`/`enabled` (kill-switch immediately before
  publish, §7), then call the adapter. Per-item `try/except` + per-item `commit` → one failure
  never blocks others (§8.3 crash isolation).
  - success → `PUBLISHED`, set `external_url`/`published_at`, record one
    `MetricEvent(stage=IMPRESSION, value=1, channel_id, content_item_id,
    source=f"publish:{idempotency_key}")` (unique `source` ⇒ metric is idempotent too).
  - `Retryable` → leave `SCHEDULED` (retried on the next tick; §8.4 heartbeat alerts on repeated
    fail, S6.2 — that is the escalation, not a hard attempt cap).
  - permanent `Exception` → `PUBLISH_FAILED` + `error`.
  - already `PUBLISHED` items are never re-selected (status guard = primary idempotency).

### 5. Wiring
- `app/scheduler.py`: add a `_publish_tick` interval job (pace + publish) alongside the crank tick,
  reusing `crank_check_interval_seconds` (no new config value needed).
- `app/channels/__init__.py`: export the adapter surface.

## Deviations / assumptions
- **Self-authored plan** — issue #23 had acceptance criteria but no plan comment.
- **Publish runs inline in a periodic pass, not as a per-item `job_run`.** The spec says "retry via
  the job_run worker loop"; an inline pass with per-item `try/except` + per-item commit is the
  lazier equivalent (at-least-once + idempotency + crash isolation) and needs no new `job_run`
  column or handler. Deviation noted; escalation for repeated transient failures is the S6.2
  heartbeat alert, matching §8.4.
- **Reddit crash-window**: PRAW submit is not natively idempotent; a crash between submit and the
  status commit could double-post. Bounded and documented as a Known Limitation (same class as the
  documented non-idempotent-cost limit). Blog publish *is* idempotent (file existence).
- **Metrics** = one `IMPRESSION` seam row per publish (reach/attribution fills in P6); satisfies
  "per-item metrics recorded" without inventing a new stage.
- No new DB migration: all columns pre-seeded (S4.2). Dev-DB recreate is the accepted v1 path.

## Tests (TDD, RED first) — `tests/test_publish.py`
- **Pacing**: `critic_passed` → `SCHEDULED` with spread `scheduled_for` + `idempotency_key`; ≤
  `daily_cap` per rolling day; successive crank batches keep spacing; paused/disabled/non-autonomous
  channels skipped; cap-unset spreads across window.
- **Publish** (stub adapter): due `SCHEDULED` → `PUBLISHED` + url + `published_at` + one
  `MetricEvent`; not-yet-due skipped; `Retryable` → stays `SCHEDULED` (retried); permanent error →
  `PUBLISH_FAILED` + error; already-`PUBLISHED` not re-published (adapter call count); paused channel
  not published (kill-switch); one failing item doesn't block a sibling.
- **BlogAdapter**: writes file into workspace site dir, returns url; re-publish overwrites (idempotent);
  `delete` removes the file.
- **RedditAdapter** (injected fake praw client): item→submission mapping, permalink returned,
  subreddit/flair from `profile_json`; PRAW error → `Retryable`.

## Gotchas (tasks/lessons.md)
- Run `black`/`ruff`/`pytest` from `backend/` (not repo root).
- Validate cheap preconditions before building the praw client (unknown-type / missing-creds must
  fail identically with or without secrets).
- No `gh pr edit --body` on this repo → REST PATCH. Verify head-SHA `mergeStateStatus == CLEAN`
  before merge.
- Keep the generate handler's S4.3/S4.4 invariants intact (this story only reads `critic_passed` rows).
