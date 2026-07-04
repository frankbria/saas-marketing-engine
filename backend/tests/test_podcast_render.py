"""S5.2: podcast music-bed mix (issue #30).

The story: given a narration MP3 and a music-bed brief, the GPU plane generates a bed (ACE-Step)
and mixes it under the voice into one mastered MP3. This runs only for episodes that opt into a
music bed — narration-only episodes never reach here (they finish on the VPS, zero GPU minutes).

`render_audio` is a PURE function (no DB, no broker, no settings) so it runs unchanged on the rented
GPU pod. These tests drive real ffmpeg — no mocking (repo policy) — and skip when the toolchain
isn't present. The narration and the (faked ACE-Step) music bed are synthesised in-test with ffmpeg
lavfi so the suite carries no binary fixtures; the music generator is injected per the issue's test
plan ("a faked ACE-Step step").
"""

import base64
import shutil
import subprocess

import pytest

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

requires_ffmpeg = pytest.mark.skipif(
    not _HAS_FFMPEG,
    reason="requires ffmpeg + ffprobe on PATH (present in the GPU image)",
)


def _tone(frequency: int, seconds: float) -> bytes:
    """Synthesise a short MP3 tone via ffmpeg lavfi (no binary fixture ships in the repo)."""
    out = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration={seconds}",
            "-f",
            "mp3",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return out.stdout


def _narration_b64(seconds: float) -> str:
    return base64.b64encode(_tone(440, seconds)).decode("ascii")


def _fake_music(*, frequency: int = 220, calls: list | None = None):
    """A stand-in ACE-Step bed generator returning a real tone at the requested duration."""

    def gen(prompt: str, duration_seconds: float) -> bytes:
        if calls is not None:
            calls.append((prompt, duration_seconds))
        return _tone(frequency, duration_seconds)

    return gen


def _probe_duration(mp3_bytes: bytes, tmp_path) -> float:
    path = tmp_path / "probe.mp3"
    path.write_bytes(mp3_bytes)
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def _is_mp3(data: bytes) -> bool:
    # An MP3 stream starts with an ID3 tag ("ID3") or an MPEG-audio frame sync (0xFF 0xEz).
    return len(data) > 4 and (data[:3] == b"ID3" or (data[0] == 0xFF and data[1] & 0xE0 == 0xE0))


@requires_ffmpeg
def test_mix_produces_playable_mp3(tmp_path):
    from app.modules.media.audio import render_audio

    b64 = render_audio(
        _narration_b64(2.0),
        "warm lo-fi bed",
        max_bytes=100 * 1024 * 1024,
        generate_music=_fake_music(),
    )
    data = base64.b64decode(b64)
    assert _is_mp3(data), "output is not a valid MP3"
    # The mix tracks the narration's length (amix duration=first), not the bed's.
    assert _probe_duration(data, tmp_path) == pytest.approx(2.0, abs=0.6)


@requires_ffmpeg
def test_bed_is_generated_for_the_narration_duration():
    from app.modules.media.audio import render_audio

    calls: list = []
    render_audio(
        _narration_b64(3.0),
        "ambient pad",
        max_bytes=100 * 1024 * 1024,
        generate_music=_fake_music(calls=calls),
    )
    assert len(calls) == 1
    prompt, duration = calls[0]
    assert prompt == "ambient pad"
    assert duration == pytest.approx(3.0, abs=0.6)  # the bed is sized to the narration


@requires_ffmpeg
def test_shorter_bed_still_covers_the_episode(tmp_path):
    # A bed shorter than the narration must not truncate the episode: amix pads with silence and
    # the output still runs the full narration length (a truncated episode is a mix bug).
    from app.modules.media.audio import render_audio

    def short_bed(prompt: str, duration_seconds: float) -> bytes:
        return _tone(220, 0.5)  # deliberately far shorter than the narration

    b64 = render_audio(
        _narration_b64(2.0), "short", max_bytes=100 * 1024 * 1024, generate_music=short_bed
    )
    assert _probe_duration(base64.b64decode(b64), tmp_path) == pytest.approx(2.0, abs=0.6)


@requires_ffmpeg
def test_output_over_max_bytes_raises():
    from app.modules.media.audio import render_audio

    with pytest.raises(ValueError, match="max_bytes"):
        render_audio(_narration_b64(1.0), "bed", max_bytes=10, generate_music=_fake_music())


@requires_ffmpeg
def test_empty_music_bed_raises():
    from app.modules.media.audio import render_audio

    with pytest.raises(RuntimeError, match="empty"):
        render_audio(
            _narration_b64(1.0),
            "bed",
            max_bytes=10_000_000,
            generate_music=lambda p, d: b"",
        )


def test_unparseable_base64_narration_raises():
    from app.modules.media.audio import render_audio

    with pytest.raises(ValueError, match="base64"):
        render_audio(
            "@@@ not base64 @@@", "bed", max_bytes=10_000_000, generate_music=_fake_music()
        )


def test_real_generate_music_fails_loudly_without_model():
    # Deferred infra: with no ACE-Step model on the worker the real seam must raise a clear
    # operator error (mirrors _real_tts without a key), never silently ship a bedless/empty mix.
    from app.modules.media.audio import _real_generate_music

    with pytest.raises(RuntimeError, match="ACE-Step"):
        _real_generate_music("bed", 3.0)


def test_render_audio_task_registered():
    # Routing is name-prefix based (celery_app task_routes "media.*"): the task must exist under
    # the media namespace or it will never reach the GPU worker. No broker needed.
    from app.celery_app import MEDIA_QUEUE, celery_app
    from app.modules.media import tasks  # noqa: F401 — importing registers the task

    assert "media.render_audio" in celery_app.tasks
    options = celery_app.amqp.router.route({}, "media.render_audio")
    assert options["queue"].name == MEDIA_QUEUE


def test_render_audio_task_shares_media_retry_contract():
    from app.modules.media.tasks import render_audio as render_task
    from app.worker import MAX_ATTEMPTS

    assert render_task.acks_late is True
    assert render_task.max_retries == MAX_ATTEMPTS - 1
