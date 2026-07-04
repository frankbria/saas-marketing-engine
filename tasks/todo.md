# Issue #29 — S5.1 Short-form video pipeline: Adapted Implementation Plan

**Branch**: `feature/issue-29-short-form-video-pipeline` · **Plan source**: issue comment, adapted to codebase 2026-07-03

## Architecture (as adapted)

Two execution planes exist; video bridges them:
- **VPS worker** = the in-process job loop (`app/worker.py`) + APScheduler ticks (`app/scheduler.py`). All CPU/API steps run here. (No default-queue Celery worker exists; S5.0 deliberately added only the GPU pod as a Celery consumer.)
- **GPU plane** = Celery `media` queue consumed by the ephemeral RunPod worker (S5.0). Tasks named `media.*` auto-route there (`app/celery_app.py:35`).

Video flow:
```
crank fan-out (YOUTUBE→VIDEO) → job_run generate
  → run_generate_video (in-process): LLM script+title → critic+safety gate → deterministic guard
    → ElevenLabs TTS (httpx) → workspace checkpoints → ContentItem(status=RENDERING)
    → dispatch media.render_video (Celery, media queue; pure fn: args in, MP4 bytes out)
  → _video_render_tick (APScheduler): poll AsyncResult → write MP4 to workspace,
    set media_ref, status=CRITIC_PASSED → existing pace→publish machinery
  → publish pass → YouTubeAdapter (httpx, resumable upload, §8.3 idempotency) → external_url
```

Key properties:
- **Cold-start**: render task parks on the broker until the provisioner boots a pod (S5.0 machinery; `acks_late` + `task_reject_on_worker_lost` requeue on teardown).
- **Resumability**: the GPU task is a pure function of its args (no DB/filesystem on the pod); all state lives VPS-side in workspace checkpoints + ContentItem. Re-runs overwrite artifacts idempotently (atomic temp+replace).
- **No double-publish**: publish is a separate stage gated by status + §8.3 remote check (idempotency marker in video description, scanned before upload).
- **Artifact transfer** pod→VPS via Celery result backend (base64 MP4, size-guarded). S5.0 left artifact return unsolved; this is the zero-new-infra default, seamed for object storage later.

## Steps

1. **Model + config seams**
   - `app/models/content_item.py`: add nullable `media_ref: str | None` (workspace-relative path); add `RENDERING` + `RENDER_FAILED` to `ContentItemStatus`; add RENDER_FAILED to `_TERMINAL_FAILURE`.
   - `app/config.py`: `elevenlabs_api_key: SecretStr|None`, `elevenlabs_voice_id`, `video_render_max_bytes`, `video_render_tick_seconds`, `video_max_render_dispatches`; register secret with vault; `.env.example` entries.
   - Tests: model/status round-trip in the generate/render tests (no separate file).

2. **Video script generation (LLM)** — `app/ai/client.py`
   - `generate_video_script(client, ...)` mirroring `generate_social_post` (client.py:506): `messages.parse` structured output → Pydantic `VideoScript{title, description, segments[{caption, narration}]}`; GEN_MODEL; cost via `cost_cents`.
   - Tests: seam-injected, no network (existing pattern).

3. **Video generator** — `app/modules/crank/generate_video.py`
   - `run_generate_video(job, session, *, generate=, critique=, tts=, dispatch_render=)` (DI seams like generate.py:205): script → critic gate on script text (S4.3, regeneration loop reusing thresholds) → `check_content` guard (S4.4) → TTS per-segment narration via ElevenLabs httpx call → write `workspace/{slug}/media/video/{job}/` checkpoints (script.json, narration.mp3; each step skips if output exists) → create ContentItem(content_type=video, body=script text, status=RENDERING, meta=render task id) → dispatch `media.render_video`.
   - `app/modules/crank/generate.py`: route content_type==video to it from the `generate` handler.
   - `app/modules/crank/crank.py`: `_CHANNEL_CONTENT_TYPES[ChannelType.YOUTUBE] = (ContentType.VIDEO,)`.
   - `app/models/channel.py`: add YOUTUBE to `AUTONOMOUS_TYPES`.
   - Tests (`tests/test_generate_video.py`): produces video item; critic-fail → CRITIC_FAILED + no dispatch; guard-fail → GUARD_FAILED; TTS via `httpx.MockTransport` fake ElevenLabs; re-run skips existing checkpoints (idempotent); budget/kill-switch parity with text path.

4. **Render task (GPU plane)** — `app/modules/media/video.py` + `tasks.py`
   - `render_video(script: dict, narration_b64: str, spec) -> bytes`: pure ffmpeg subprocess composition — caption slides (drawtext) timed to narration, mux to MP4. No DB, no workspace.
   - `media.render_video` task in `app/modules/media/tasks.py` following `media.probe` template (acks_late, max_retries=MAX_ATTEMPTS-1, retry_backoff).
   - `infra/gpu-worker/Dockerfile`: add `fonts-dejavu-core`.
   - Tests (`tests/test_video_render.py`): tiny real render (skip if ffmpeg absent); determinism/purity; size guard.

5. **Render-complete tick** — `app/modules/crank/video_pipeline.py` + `app/scheduler.py`
   - `advance_video_renders(session, now)`: for RENDERING items — poll AsyncResult by stored task id; ready → decode, size-check, atomic-write MP4 to workspace, set `media_ref`, status=CRITIC_PASSED; failed/lost → bounded re-dispatch (meta counter, max from settings) else RENDER_FAILED + error. Never raises (tick contract).
   - Register `_video_render_tick` in scheduler like `_media_provisioner_tick`.
   - Tests: injected result-getter seam; success/failure/re-dispatch-bound paths.

6. **YouTube adapter** — `app/channels/youtube.py`
   - Follows reddit.py shape: `credential_key="youtube_oauth"`, lazy httpx client seam `_build_youtube(creds)`, `_is_transient`/`_is_auth_failure` (5xx/timeouts → Retryable, 401 → AuthFailure, 403 quota → Retryable).
   - `publish`: fail closed on missing idempotency_key/media_ref; §8.3 remote check — list own recent uploads, match `sme-ref:{key}` marker in description → return existing URL; else resumable upload (init → PUT chunks) of workspace file, title/description from item, marker appended; return `watch?v=` URL.
   - `delete`: parse video id from external_url, `videos.delete`, already-gone = no-op.
   - Register in `get_adapter` (base.py); add YOUTUBE `OAuthProvider` to `OWNED_TOKEN_PROVIDERS` (oauth_refresh.py) — token URL `https://oauth2.googleapis.com/token`, upload+readonly scopes; verify client auth style (body, not Basic) for Google.
   - **Direct httpx, no google-api-python-client** — matches repo test pattern and the issue's "stub YouTube server" test plan.
   - Tests (`tests/test_youtube_adapter.py`): stub YouTube via `httpx.MockTransport` — happy path, idempotent re-publish (no second upload), transient→Retryable, 401→AuthFailure→fence, missing key/file fails closed, retract, resumable-upload chunk resume.

7. **Integration: media queue cold-start + pipeline** — `tests/test_media_video_queue.py`
   - Copy `test_media_queue.py:82` pattern: enqueue `media.render_video` with no worker → `media_queue_depth()==1` → `start_worker(queues=[media])` → result completes (requires_redis + ffmpeg-skip).
   - Full generate→gate→render-tick→pace→publish flow against stub YouTube (real SQLite, seams at HTTP boundaries only).

## Acceptance criteria → test map

- [ ] `video` generator produces `content_item(content_type=video)` → step 3 tests
- [ ] Passes critic+safety (S4.3) + deterministic guard (S4.4) → step 3 gate tests
- [ ] Publishes via YouTube Data API; long jobs retry without blocking → step 6 tests + Retryable-keeps-scheduled publish-pass test
- [ ] GPU steps on `media` queue survive cold-start → step 7 cold-start test + routing assertion
- [ ] Resumable/idempotent across teardown; re-run never double-publishes (§8.3) → step 3 checkpoint-skip tests, step 5 re-dispatch test, step 6 idempotency test
- [ ] CPU/API steps on VPS worker; only GPU-bound steps rent compute → design + routing test (only `media.render_video` dispatched to Celery)

## Deviations from the issue plan (autonomous decisions)

1. **Renderer = ffmpeg caption-composition** (subprocess, pure fn) instead of manim/Remotion/video-podcast-maker — the gpu-worker image already ships ffmpeg; heavier renderers slot into the same seamed `media.render_video` task later (matches the S5.0 `media.probe` plumbing-first philosophy and TECH_SPEC §10 subprocess pattern). Render routed to the media queue per the issue's own plan, though the v1 impl is CPU-feasible — it's the step that becomes GPU-bound with real renderers.
2. **YouTube via direct httpx** (no google SDK) — resumable upload protocol is plain HTTP; matches `httpx.MockTransport` testing and keeps pinned deps minimal.
3. **Artifact return via Celery result backend** (base64, size-guarded) — no new storage service; noted as Known Limitation.
4. **CPU steps on the in-process worker + APScheduler tick**, not a default-queue Celery worker (none exists; adding one would duplicate execution planes).
5. New `RENDERING`/`RENDER_FAILED` statuses (additive str-enum values, no migration) keep unfinished videos out of pacing; render completion promotes to `CRITIC_PASSED` so pace/publish stay untouched.
