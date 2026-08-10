# S6.2.1 — Real reach ingestion; make the zero-reach alert able to fire (issue #79)

**Branch:** `feature/issue-79-real-reach-ingestion` · **Plan source:** self-authored.

## The defect

`publish_scheduled` writes one `MetricEvent(stage=IMPRESSION, value=1)` per published item
(`crank/publish.py:249`) and `heartbeat._reach()` sums those same rows (`heartbeat.py:64`). Nothing
polls any platform. So:

- the funnel dashboard's "impressions" is a publish counter, and
- the zero-reach alert **cannot fire**: `published_in_window > 0` guarantees `reach >= 1` over the
  same window, because publishing *is* what writes the reach row.

PRD §12 names cold-account shadowbanning as risk #1 and zero-reach alerting as the mitigation.
DoD-2 requires "heartbeat confirming non-zero reach".

## Architectural decision (Phase 4 — approved by maintainer)

Platform counters are **cumulative gauges**; `metric_event` is **append-only**, summed over a
window. Storing gauges directly would double-count and make the windowed sum meaningless.

**Chosen: delta events.** Each poll inserts a REACH row carrying the increase since the previous
poll. Preserves the append-only contract every other stage follows and keeps `_reach(since, now)`
meaning literally "reach gained in this window" — which is exactly the question the shadowban alert
asks. Rejected: cumulative upsert (mutates an append-only table; forces rewriting the alert to be
item-scoped; loses history).

## Steps (TDD — test first for each)

1. **`MetricStage.REACH`** (`models/metric_event.py`) + note that `value` is a *delta* for this
   stage, unlike the count/cents convention of the others.
2. **Adapter seam** (`channels/base.py`): `fetch_reach(item, product, channel, creds) ->
   int | None` on the protocol, returning the platform's **cumulative** count, **plus** a static
   `has_platform_reach: bool`. Two mechanisms because there are two questions: the heartbeat needs
   "can this channel type be shadowban-checked at all?" without holding an item or a credential
   (that's the flag), while the poll needs "what is the number right now?" (that's the return, where
   `None` = no number this tick — a deleted post or a drifted payload — distinct from a real zero).
   Blog and podcast declare `has_platform_reach = False`.
3. **Reddit** (`channels/reddit.py`): `submission.score` via PRAW; transient errors → skip, not
   raise. **YouTube** (`channels/youtube.py`): `videos.list?part=statistics` → `viewCount`; reuse
   the existing quota/`_raise_for_status` handling.
4. **Poll pass** (`modules/metrics/reach.py`): `poll_reach(session, now, *, adapter_for=)`
   — select `published` items on enabled/autonomous/non-failed channels published within the reach
   window; per item compute `delta = max(0, cumulative - sum(prior REACH deltas))`; insert one row
   with `source=f"reach:{item.id}:{now.isoformat()}"`. Bounded, per-item try/except, never raises
   (mirrors `publish_scheduled`'s isolation). `max(0, …)` guards a counter reset/deletion.
5. **Scheduler tick** (`scheduler.py`) + `reach_poll_interval_seconds` in config (bounded `ge=`).
6. **Heartbeat** (`modules/heartbeat.py`): `_reach()` reads `MetricStage.REACH`; the zero-reach
   evaluation **skips channels with no platform counter** so blog/podcast never false-alarm.
7. **Funnel rollup** (`modules/metrics/funnel.py`): expose `reach` as its own stage alongside
   `impressions`, so the dashboard stops presenting a publish count as reach.
8. **Dashboard** (`lib/api.ts` + funnel component): surface `reach` distinctly from `impressions`.

## Acceptance criteria (from #79)

- [x] Periodic poll fetches real engagement (Reddit `score`, YouTube `viewCount`)
      — `test_reach_adapters.py`
- [x] Real reach stored under a stage distinct from the publish counter — `MetricStage.REACH`
- [x] `heartbeat._reach()` reads the real-reach stage; zero-reach can fire for a channel that
      published but earned nothing
- [x] **Test proving the alert fires** —
      `test_reach_poll.py::test_zero_reach_alert_fires_for_a_published_post_nobody_saw`, which
      drives `publish_scheduled` → `poll_reach` → `evaluate_alerts` rather than hand-building the
      alert's input. The pre-existing `test_alert_zero_reach_when_published_but_no_impressions`
      passed against the broken code precisely because it hand-built a state the publish pass
      could never produce.
- [x] Funnel rollup distinguishes published from reach — `stages.reach`, and the dashboard now
      labels the publish counter "Published" instead of "Impressions"
- [x] Poll is bounded, never raises, no-ops on an unconfigured/failed channel
- [x] Owned channels explicitly excluded from zero-reach rather than silently passing

## Known limitations (for the PR)

- Reddit `score` is net upvotes, not impressions — the closest cheap proxy PRAW exposes without
  mod-only insights. Zero score on a published post is still the shadowban signal we want.
- No backfill for items published before this lands; their first poll records the full cumulative
  count as one delta. Harmless for the alert (it asks "was there any reach", not "how much"), but a
  one-off spike in the funnel rollup the first time the poll runs.
- Historic `IMPRESSION` rows are left untouched. They remain an honest publish count under a
  misleading key name; renaming the wire field is a dashboard-contract change worth its own issue.
- Reddit `score` can be negative on a heavily downvoted post; `max(0, …)` floors the delta, so a
  post that gets downvoted after earning reach never subtracts from the window.
- **The alert has no settling grace period.** `evaluate_alerts` fires when a channel published
  anything in the window and earned zero reach across it — so a brand-new channel whose first post
  is a few hours old can trip it before the post has had a fair chance. This is pre-existing
  semantics that were simply unreachable before; making the alert fireable exposes it for the first
  time. Bounded in practice (the digest is idempotent per UTC day, so at most one alert/day, and any
  single unit of reach on any post in the window clears it). Worth a follow-up if it proves noisy:
  either a minimum post age or requiring N consecutive zero-reach days.
