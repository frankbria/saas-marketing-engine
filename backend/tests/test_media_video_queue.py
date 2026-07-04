"""S5.1: video render on the real `media` queue + full pipeline integration (issue #29).

The cold-start AC rides the S5.0 machinery: a render enqueued with NO worker up must park on the
broker (nothing blocks, nothing is lost) and complete once a worker joins — exactly how the
ephemeral GPU pod appears after the provisioner boots it. Real Redis + a real Celery worker (repo
policy: no mocking of Redis/Celery); skips when no broker is reachable — CI provides one. The
render itself needs ffmpeg + a DejaVu font on the host, so those gate the test too.

The end-to-end test (generate → gates → tick dispatch → real queue render → collect → pace →
publish) exercises every seam with only the true network boundaries stubbed (LLM/TTS callables,
YouTube behind httpx.MockTransport) — DB, workspace, broker, and worker are all real.
"""

import base64
import shutil
import subprocess

import pytest
import redis

from app.celery_app import MEDIA_QUEUE, celery_app
from app.config import settings
from app.modules.media.queue import media_queue_depth


def _redis_available() -> bool:
    try:
        redis.Redis.from_url(settings.celery_broker_url, socket_connect_timeout=1).ping()
        return True
    except (redis.exceptions.RedisError, OSError):
        return False


requires_redis = pytest.mark.skipif(
    not _redis_available(),
    reason="requires Redis at SME_CELERY_BROKER_URL (start infra/compose.dev.yml)",
)
requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="requires ffmpeg/ffprobe on PATH (the GPU worker image ships them)",
)


def _drain_media_queue() -> None:
    redis.Redis.from_url(settings.celery_broker_url).delete(MEDIA_QUEUE)


def _tiny_narration_b64(seconds: float = 1.0) -> str:
    """Synthesize a short MP3 with ffmpeg itself — no fixture binaries in the repo."""
    out = subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-q:a",
            "9",
            "-f",
            "mp3",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return base64.b64encode(out.stdout).decode()


_SCRIPT = {
    "title": "Cold start",
    "description": "Survives the provisioner gap.",
    "segments": [{"caption": "Waits on the broker", "narration": "Then renders."}],
    "pillar": "onboarding",
}


@requires_redis
@requires_ffmpeg
def test_render_enqueued_with_no_worker_completes_once_worker_joins():
    # Cold-start AC (issue #29): the video render must tolerate the S5.0 provisioner gap — a
    # job enqueued while no GPU worker exists parks on the broker and completes when one boots.
    from celery.contrib.testing.worker import start_worker

    from app.modules.media.tasks import render_video

    _drain_media_queue()
    try:
        result = render_video.delay(_SCRIPT, _tiny_narration_b64(), max_bytes=50 * 1024 * 1024)
        assert media_queue_depth() == 1  # parked, no worker yet — and nothing raised

        with start_worker(celery_app, queues=[MEDIA_QUEUE], perform_ping_check=False):
            mp4 = base64.b64decode(result.get(timeout=60))
        assert mp4[4:8] == b"ftyp"  # a real MP4 came back through the result backend
        assert media_queue_depth() == 0
    finally:
        _drain_media_queue()
