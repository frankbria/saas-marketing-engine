"""S5.2: podcast music-bed mix on the GPU plane (issue #30).

`render_audio` is the *only* GPU step of the podcast pipeline, and it runs solely when an episode
opts into a music bed. It generates a music bed for the episode (ACE-Step, the GPU model) and mixes
it under the already-narrated track, returning one mastered MP3. Like `media.video.render_video`
it is a **pure** function — no DB, no broker, no app settings — so it runs unchanged on the
ephemeral rented GPU pod, which must not depend on VPS config (TECH_SPEC Phase B). The caller
(`media.render_audio` on the GPU `media` queue) passes `max_bytes` in explicitly for that reason.

A narration-only episode never reaches this module: the generate handler finalizes it in-process on
the VPS (the narration MP3 *is* the episode), so it costs zero GPU minutes. This module exists only
for the music-bed path.

Music generation is the SEAM (`generate_music`): v1 wires ACE-Step behind this signature and fails
loudly if the on-pod model is absent (deferred infra — the GPU image pins it later, mirroring how
`_real_tts` fails loudly without an API key). Tests inject a fake bed, exactly as the issue's test
plan prescribes ("a faked ACE-Step step"). Output is a base64 string because Celery's JSON
serializer can't carry raw bytes.
"""

from __future__ import annotations

import base64
import binascii
import os
import subprocess
import tempfile
from collections.abc import Callable

# generate_music(prompt, duration_seconds) -> music-bed audio bytes (any ffmpeg-decodable format).
MusicFn = Callable[[str, float], bytes]

# Hard bounds on the subprocess calls: the worker runs --concurrency=1, so a hung ffmpeg/ffprobe
# would wedge the whole pod — and a busy-looking worker is never torn down by the provisioner,
# turning one bad mix into unbounded paid GPU time. TimeoutExpired propagates like any other failure
# (Celery autoretry → bounded by the tick's dispatch cap). Generous margins for a multi-minute mix.
_PROBE_TIMEOUT_SECONDS = 60
_MIX_TIMEOUT_SECONDS = 600

# The music bed sits well under the voice so narration stays intelligible (a simple, robust duck —
# a constant attenuation rather than sidechain compression, which is fragile across ffmpeg builds).
_BED_VOLUME = 0.18


def _real_generate_music(prompt: str, duration_seconds: float) -> bytes:
    """Generate a music bed via the on-pod ACE-Step model (the podcast pipeline's only GPU step).

    Deferred infra in v1: the ACE-Step model is not yet pinned into the GPU worker image (see
    infra/gpu-worker/Dockerfile), so this raises loudly when the model is absent — the same
    fail-when-unconfigured contract as `_real_tts` without an API key. The music-bed path is wired
    end-to-end (task, mix, tick, publish); only the model install is outstanding. Tests inject a
    fake bed via the `generate_music` seam, so nothing here needs a real GPU to be exercised."""
    try:
        import acestep  # type: ignore  # noqa: F401 — presence check; the pin lands with the image
    except ImportError as exc:  # pragma: no cover - exercised only on a pod without the model
        raise RuntimeError(
            "ACE-Step is not installed on this worker; cannot generate a podcast music bed. "
            "Pin the model into the GPU worker image (infra/gpu-worker/Dockerfile) to enable the "
            "music-bed path — narration-only episodes need no music and never reach this step."
        ) from exc
    # pragma: no cover - real model call lands with the image pin (deferred infra)
    from acestep import generate_music_bed  # type: ignore

    return generate_music_bed(prompt=prompt, duration_seconds=duration_seconds)


def _probe_duration(path: str) -> float:
    """Return the audio duration in seconds via ffprobe, raising on unreadable input."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"ffprobe could not read narration: {proc.stderr.strip()[-500:]}")
    return float(proc.stdout.strip())


def render_audio(
    narration_b64: str,
    music_prompt: str,
    *,
    max_bytes: int,
    generate_music: MusicFn | None = None,
) -> str:
    """Mix a narration track under a generated music bed into one mastered MP3, base64-encoded.

    `narration_b64` is the ElevenLabs narration (base64 MP3); `music_prompt` briefs the music bed.
    The bed is generated to the narration's duration, ducked well under the voice, mixed for exactly
    the narration's length (`amix duration=first`), and loudness-normalised. Raises ``ValueError``
    for empty/invalid narration or output over ``max_bytes``; ``RuntimeError`` if ffmpeg/ffprobe
    fail or the music generator is unavailable.

    `generate_music` is resolved at call time (default: the on-pod ACE-Step seam) so a faked bed can
    be injected — tests pass one directly, and the in-process Celery worker picks up a monkeypatched
    module seam.
    """
    gen = generate_music if generate_music is not None else _real_generate_music
    try:
        narration = base64.b64decode(narration_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"narration_b64 is not valid base64: {exc}") from exc
    if not narration:
        raise ValueError("narration_b64 decoded to empty audio")

    with tempfile.TemporaryDirectory() as tmp:
        narration_path = os.path.join(tmp, "narration.mp3")
        with open(narration_path, "wb") as fh:
            fh.write(narration)

        duration = _probe_duration(narration_path)

        music = gen(music_prompt, duration)
        if not music:
            raise RuntimeError("music generator returned empty audio")
        music_path = os.path.join(tmp, "bed.audio")
        with open(music_path, "wb") as fh:
            fh.write(music)

        out_path = os.path.join(tmp, "episode.mp3")
        # Duck the bed under the voice, mix for the narration's length (silence-padded if the bed is
        # short, trimmed if long), then loudness-normalise the result to a broadcast-ish target.
        filter_complex = (
            f"[1:a]volume={_BED_VOLUME}[bed];"
            "[0:a][bed]amix=inputs=2:duration=first:dropout_transition=0[mix];"
            "[mix]loudnorm=I=-16:TP=-1.5:LRA=11[out]"
        )
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            narration_path,
            "-i",
            music_path,
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            # Strip metadata for a reproducible output (no encoder timestamps/tags).
            "-map_metadata",
            "-1",
            out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=_MIX_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg audio mix failed (code {proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace').strip()[-1000:]}"
            )

        size = os.path.getsize(out_path)
        if size > max_bytes:
            raise ValueError(f"mixed episode {size} bytes exceeds max_bytes {max_bytes}")

        with open(out_path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")
