"""S5.1: short-form video render (issue #29).

The story: given a script (title + captioned segments) and a narration MP3, the GPU plane
composes a vertical (1080x1920) MP4 — a solid background with each segment's caption burned
in over its share of the narration, muxed with the audio. This is the seam where a real
renderer (manim/Remotion) slots in later; v1 proves the ffmpeg composition end-to-end.

render_video is a PURE function (no DB, no broker, no settings) so it can run unchanged on
the rented GPU pod, which must not depend on VPS config. These tests drive real ffmpeg —
no mocking (repo policy) — and skip when the toolchain isn't present. The narration is
synthesised in-test with ffmpeg lavfi so the suite carries no binary fixtures.
"""

import base64
import shutil
import subprocess

import pytest

# ffmpeg/ffprobe do the actual work; a candidate font must exist for drawtext to render.
# Without any of these the render can't run, so skip rather than fail — CI's GPU image
# (infra/gpu-worker) ships all three.
_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]
_HAS_FONT = any(__import__("os").path.exists(p) for p in _FONT_CANDIDATES)

requires_toolchain = pytest.mark.skipif(
    not (_HAS_FFMPEG and _HAS_FONT),
    reason="requires ffmpeg + ffprobe on PATH and a DejaVu font (present in the GPU image)",
)


def _make_narration(seconds: float) -> str:
    """Synthesise a short MP3 tone and return it base64-encoded (the render's input shape).

    Built in-test via ffmpeg lavfi so no binary fixture ships in the repo.
    """
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
            f"sine=frequency=440:duration={seconds}",
            "-f",
            "mp3",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return base64.b64encode(out.stdout).decode("ascii")


def _probe_duration(mp4_bytes: bytes, tmp_path) -> float:
    path = tmp_path / "probe.mp4"
    path.write_bytes(mp4_bytes)
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


def _is_mp4(data: bytes) -> bool:
    # An MP4/ISO-BMFF file carries the "ftyp" box type at bytes 4:8 of the first box.
    return len(data) > 8 and data[4:8] == b"ftyp"


@requires_toolchain
def test_render_produces_playable_mp4():
    from app.modules.media.video import render_video

    script = {"title": "Hello", "segments": [{"caption": "One", "narration": "one"}]}
    b64 = render_video(script, _make_narration(1.0), max_bytes=50 * 1024 * 1024)

    data = base64.b64decode(b64)
    assert len(data) > 0
    assert _is_mp4(data), "output is not a valid MP4 (no ftyp box)"


@requires_toolchain
def test_two_segments_span_the_narration(tmp_path):
    from app.modules.media.video import render_video

    # Two captions each render over half the narration; the muxed output length must track
    # the narration (with -shortest) — a truncated or over-long video is a composition bug.
    script = {
        "title": "Two",
        "segments": [
            {"caption": "First half", "narration": "a"},
            {"caption": "Second half", "narration": "b"},
        ],
    }
    b64 = render_video(script, _make_narration(2.0), max_bytes=50 * 1024 * 1024)
    duration = _probe_duration(base64.b64decode(b64), tmp_path)
    assert duration == pytest.approx(2.0, abs=0.5)


@requires_toolchain
def test_output_over_max_bytes_raises():
    from app.modules.media.video import render_video

    # max_bytes caps what the VPS will pull back from the pod; a real render blowing the cap
    # must fail loudly, not silently ship a truncated/oversized asset. 10 bytes is
    # unreachable for any MP4, so this always trips.
    script = {"title": "Big", "segments": [{"caption": "X", "narration": "x"}]}
    with pytest.raises(ValueError, match="max_bytes"):
        render_video(script, _make_narration(1.0), max_bytes=10)


@requires_toolchain
def test_empty_segments_raises():
    from app.modules.media.video import render_video

    # No segments = nothing to caption; a silent empty/black video would masquerade as
    # success downstream. Fail at the door.
    with pytest.raises(ValueError, match="segment"):
        render_video({"title": "Empty", "segments": []}, _make_narration(1.0), max_bytes=10_000_000)


@requires_toolchain
def test_unparseable_base64_narration_raises():
    from app.modules.media.video import render_video

    # Not even valid base64 — must fail at the door with a clear ValueError, never a video
    # with no/broken audio that looks fine until someone plays it.
    script = {"title": "Bad", "segments": [{"caption": "X", "narration": "x"}]}
    with pytest.raises(ValueError, match="base64"):
        render_video(script, "@@@ not valid base64 @@@", max_bytes=10_000_000)


@requires_toolchain
def test_valid_base64_non_audio_narration_raises():
    from app.modules.media.video import render_video

    # The subtler failure: perfectly valid base64 that decodes to bytes ffprobe can't read as
    # audio. This exercises the ffprobe-failure path (distinct from the decode-failure path
    # above) and must still fail loudly rather than emit a silent/audioless clip.
    junk = base64.b64encode(b"this is not an mp3 file at all").decode("ascii")
    script = {"title": "Bad", "segments": [{"caption": "X", "narration": "x"}]}
    with pytest.raises(RuntimeError, match="ffprobe"):
        render_video(script, junk, max_bytes=10_000_000)


def test_render_video_task_registered():
    # Routing is name-prefix based (celery_app task_routes "media.*"): the task must exist
    # under the media namespace or it will never reach the GPU worker. No broker needed —
    # importing the app registers the task in the registry.
    from app.celery_app import MEDIA_QUEUE, celery_app
    from app.modules.media import tasks  # noqa: F401 — importing registers the task

    assert "media.render_video" in celery_app.tasks
    options = celery_app.amqp.router.route({}, "media.render_video")
    assert options["queue"].name == MEDIA_QUEUE


def test_render_video_task_shares_media_retry_contract():
    # Same acks_late + retry budget as media.probe: a pod lost mid-render re-delivers instead
    # of dropping the job (S5.0 contract, carried forward to real media tasks).
    from app.modules.media.tasks import render_video as render_task
    from app.worker import MAX_ATTEMPTS

    assert render_task.acks_late is True
    assert render_task.max_retries == MAX_ATTEMPTS - 1
