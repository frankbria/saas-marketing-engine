# S5.2 — Podcast/audio pipeline (issue #30)

**Branch:** `feature/issue-30-podcast-audio-pipeline` · **Plan source:** issue comment, adapted to codebase.

## Design (adapted from S5.1, one meaningful divergence)

S5.1's expensive step (ffmpeg render) is GPU-bound → *every* video dispatches to the `media` queue.
S5.2's expensive step (ElevenLabs TTS) is an **API call on the VPS** → a plain episode never needs
the GPU pod. So the default inverts:

- **No music bed (default):** generate → gates → TTS → the narration MP3 **is** the episode →
  `media_ref` set + `CRITIC_PASSED`, entirely in-process on the VPS. **Never touches the `media`
  queue → zero GPU minutes.** (Satisfies the headline AC.)
- **Music bed (opt-in via channel `profile_json.music_bed=true`):** generate → gates → TTS →
  checkpoint narration + `music_prompt` → `RENDERING`; the podcast render tick dispatches
  `media.render_audio` (ACE-Step music gen + ffmpeg mix, on the GPU pod) → collect → `CRITIC_PASSED`.
  Mirrors S5.1's dispatch/collect tick exactly.

**ACE-Step** is deferred infra (Dockerfile comment). v1 ships the full plane + task + mix + an
injectable `_generate_music` seam whose real impl fails loudly if the on-pod model is absent —
exactly the `_real_tts` "raise when unconfigured" pattern. Tests fake the seam (per plan step 5).

**Publish:** owned RSS feed on the nginx static site (plan's chosen v1 channel; no OAuth). New
`ChannelType.PODCAST` (autonomous, owned infra like BLOG). `PodcastAdapter` copies the episode MP3
into `site/podcast/`, writes a per-episode sidecar JSON, (re)builds `feed.xml` (RSS 2.0 + iTunes
tags) from all sidecars in the dir — session-free, filesystem-is-truth like `BlogAdapter`.
`external_url` = episode page.

**No new content_item column / no migration:** reuse `RENDERING`/`RENDER_FAILED`, `media_ref`,
`external_url`, `meta_json`. Calendar/spot-check are content_type-agnostic → no changes.

## Steps (TDD — test first for each)

1. **AI schema + generator** (`app/ai/client.py`): `PodcastScript` (title, description, segments,
   pillar, `music_prompt: str | None`), `generate_podcast_script(...)`, `GEN_PODCAST_MAX_TOKENS`.
   Tests: parse path + refusal path.
2. **Config** (`app/config.py` + `.env.example`): `podcast_render_max_bytes`,
   `podcast_render_tick_seconds`, `podcast_max_render_dispatches` (mirror `video_*`). Reuse
   `elevenlabs_*`. Bounded `Field(ge=...)`.
3. **Channel type** (`app/models/channel.py`): `ChannelType.PODCAST`, add to `AUTONOMOUS_TYPES`.
   (`app/modules/crank/crank.py`): `_CHANNEL_CONTENT_TYPES[PODCAST] = (ContentType.PODCAST,)`.
4. **Generate handler** (`app/modules/crank/generate_podcast.py`): mirror `generate_video.py` —
   `run_generate_podcast(job, session, *, generate=, critique=, tts=, sample=)`, gate loop,
   checkpoint (`script.json`, `narration.mp3`), music-toggle branch (RENDERING vs finalize),
   `run_generate_podcast_job` wrapper + `_GENERATE/_CRITIQUE/_TTS` seams. Route in
   `generate.py:_generate_handler` (add PODCAST branch, lazy import).
   Tests (`tests/test_generate_podcast.py`): no-music finalizes to CRITIC_PASSED with poisoned
   dispatch never called; music path → RENDERING; gate-fail → no GPU, no workspace; TTS httpx
   monkeypatch (URL/headers/body) + absent-key raise; resume-from-checkpoint.
5. **Pure audio render** (`app/modules/media/audio.py`): `render_audio(narration_b64, music_prompt,
   *, max_bytes) -> str` — `_generate_music` seam (real impl invokes on-pod ACE-Step, raises loudly
   if absent) → ffmpeg mix (narration over ducked bed, normalize, `-map_metadata -1`) → size-check →
   b64 MP3. Pure: no DB/broker/settings.
   Tests (`tests/test_podcast_render.py`): real ffmpeg mix with a faked `_generate_music`
   (skips if ffmpeg absent, no-mock policy); empty/oversized raise.
6. **Media task** (`app/modules/media/tasks.py`): register `media.render_audio` (thin lazy wrapper
   → pure `render_audio`, `max_bytes` arg).
7. **Render tick** (`app/modules/crank/podcast_pipeline.py`): `advance_podcast_renders(session, now,
   *, send=, poll=)` mirroring `video_pipeline.py` (dispatch/poll/collect, bounded re-dispatch,
   never-raise). Wire `_podcast_render_tick` into `app/scheduler.py`.
   Tests (`tests/test_media_podcast_queue.py`): dispatch/collect via injected send/poll; bounded
   re-dispatch → RENDER_FAILED; real Redis+worker cold-start (`@requires_redis`/`@requires_ffmpeg`)
   proving parked→booted→collected.
8. **Publish adapter** (`app/channels/podcast.py`): `PodcastAdapter` (type=PODCAST,
   credential_key=None) publish → copy MP3 + sidecar + rebuild feed.xml; delete → remove pair +
   rebuild. Register in `app/channels/base.py:get_adapter`.
   Tests (`tests/test_podcast_adapter.py`): publish writes mp3+feed.xml with the episode enclosure;
   re-publish idempotent; delete prunes + rebuilds; feed is well-formed XML with required tags.
9. **Zero-GPU integration proof** (`tests/test_gpu_orchestrator.py` or podcast queue test): a
   no-music episode run asserts `provider.ensure_calls == 0` (cold path never builds a provider).

## Acceptance criteria (from issue #30)
- [ ] `podcast` generator (ElevenLabs/ACE-Step) producing `content_item(content_type=podcast)`
- [ ] Gated (critic+safety+guard) + published; long jobs resumable across teardown (idempotent §8.3)
- [ ] GPU-bound ACE-Step on the `media` queue survives provisioner cold-start; API-bound ElevenLabs
      on the VPS worker; **an episode with no music bed requires zero GPU minutes**

## Known limitations (for PR)
- ACE-Step on-pod model invocation is a loud-fail seam; installing the model in the GPU image is
  deferred infra (Dockerfile already notes S5.2 will pin it). The no-GPU path is fully functional.
- Episode audio rides back through the Redis result backend base64-encoded, capped by
  `podcast_render_max_bytes` (same v1 transfer limit as video; object store swaps in behind send/poll).
- RSS is the owned v1 channel; podcast-directory submissions (Apple/Spotify) are a human checklist.
