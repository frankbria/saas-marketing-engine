# S4.6 — Per-channel kill switch (#24)

**Branch:** `feat/s4.6-kill-switch` · **Plan source:** self-authored (issue had ACs only)

## What already exists (pre-seeded in S4.5)
- `Channel.paused: bool = False` — `backend/app/models/channel.py:50`
- `publish_scheduled` re-checks `channel.paused` immediately before publish — `publish.py:129` (AC #1 ✅ engine-side)
- `pace_content` excludes paused channels — `publish.py:71`
- Engine behavior already tested: `test_publish_paused_channel_kill_switch`, `test_pace_skips_paused_disabled_and_manual_channels`, autonomy-off halt (`tests/test_publish.py`)
- Frontend `Channel.paused: boolean` type — `dashboard/lib/api.ts:88`

## What's missing (the actual S4.6 deliverable)
The operator control surface to *flip* the switch. No endpoint sets `paused`; no dashboard toggle.

## Plan (TDD)

### Step 1 — Backend pause/resume endpoint
- `backend/app/api/private/channels.py`: add `PATCH /{product_id}/{channel_id}/pause` with body `{paused: bool}` (new `PauseRequest` model), mirroring the existing checklist-toggle handler. Validate product + channel-belongs-to-product (404 otherwise), set `channel.paused`, bump `updated_at`, commit, return `Channel`.

### Step 2 — Backend tests (RED first)
- `backend/tests/test_channels_api.py`: `test_pause_and_resume_channel` (PATCH true → `paused=true`; PATCH false → `paused=false`), `test_pause_wrong_channel_404`.
- End-to-end round-trip proving AC #2 via the real publish pass (in `test_publish.py`, reusing helpers): seed a due scheduled item → set `paused=True` → `publish_scheduled` skips (stays `scheduled`) → set `paused=False` → `publish_scheduled` publishes. (The API-driven flip is covered by the API test; this proves the halt/resume semantics.)

### Step 3 — Frontend API client + toggle UI
- `dashboard/lib/api.ts`: add `setChannelPaused(productId, channelId, paused)` → `PATCH /channels/{id}/{cid}/pause`, mirroring `setChecklistItemStatus`.
- `dashboard/app/products/[id]/channel-setup.tsx`: add a per-channel Pause/Resume control using the existing `run()` helper; show paused state visually.

### Step 4 — Frontend test
- `dashboard/lib/api.test.ts`: `setChannelPaused` PATCHes the pause endpoint with `{paused}` body.

## Acceptance criteria
- [ ] `channel.paused` checked immediately before every publish — **already satisfied** (publish.py:129); no regression
- [ ] Pause halts new publishes within one cycle; resume restores schedule — proven by round-trip test (Step 2)
- [ ] Dashboard toggle — Step 3

## Notes / assumptions
- No new columns → no DB-recreate concern (lessons.md: v1 has no migrations). `paused` already shipped in S4.5.
- Endpoint is unauthenticated like the rest of `/api/private` (v1 dashboard-trusted). No new auth in scope.
- Reuse `PATCH` toggle pattern; no new dependency (ponytail).
- Run black/ruff/pytest from `backend/`, not repo root (lessons.md).
