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
import json
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import redis
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.ai.client import BrandKit, CriticVerdict, VideoScript, VideoSegment, VoiceDescriptor
from app.celery_app import MEDIA_QUEUE, celery_app
from app.config import settings
from app.models import (
    Channel,
    ChannelType,
    ConnectState,
    ContentItem,
    ContentItemStatus,
    LifecycleState,
    Product,
    StrategyBrief,
)
from app.modules.crank.crank import ContentType
from app.modules.crank.generate_video import run_generate_video
from app.modules.crank.publish import pace_content, publish_scheduled
from app.modules.crank.video_pipeline import advance_video_renders
from app.modules.media.queue import media_queue_depth
from app.secrets import vault
from app.worker import enqueue


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


# --- full pipeline: generate → gates → queue render → collect → pace → publish -------------------


@pytest.fixture
def session(tmp_path):
    db = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@requires_redis
@requires_ffmpeg
def test_full_video_pipeline_generate_to_publish(session, tmp_path, monkeypatch):
    # Every seam end-to-end with only the true network boundaries stubbed: LLM + TTS are injected
    # callables, YouTube is a fake API behind httpx.MockTransport — the DB, workspace, Redis
    # broker, Celery worker, and the ffmpeg render are all real.
    from celery.contrib.testing.worker import start_worker

    from tests.test_youtube_adapter import _FakeYouTubeApi

    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "ws"))
    monkeypatch.setattr(vault.settings, "vault_key", vault.generate_key())

    product = Product(
        name="Acme",
        slug="live",
        lifecycle_state=LifecycleState.LIVE,
        brand_json=BrandKit(
            name="Acme",
            tone="confident",
            voice_descriptors=[VoiceDescriptor(descriptor="clear", guidance="short")],
            visual_seeds=["indigo"],
        ).model_dump_json(),
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    session.add(
        StrategyBrief(
            product_id=product.id,
            icp_json="{}",
            pain_points_json="[]",
            positioning="Fastest way to X.",
            channel_plan_json="[]",
            content_pillars_json=json.dumps(["onboarding"]),
            cadence_json="{}",
        )
    )
    channel = Channel(
        product_id=product.id,
        type=ChannelType.YOUTUBE,
        enabled=True,
        autonomous=True,
        connect_state=ConnectState.CONNECTED,
    )
    session.add(channel)
    session.commit()
    session.refresh(channel)
    vault.put_credential(session, product.id, "youtube_oauth", "tok", channel_id=channel.id)

    # 1) generate: script (stub LLM) → gates → TTS (stub) → `rendering` + workspace checkpoints
    job = enqueue(
        session,
        "generate",
        product_id=product.id,
        channel_id=channel.id,
        content_type=ContentType.VIDEO.value,
    )
    script = VideoScript(
        title="Why Acme wins",
        description="A quick tour.",
        segments=[VideoSegment(caption="Meet Acme", narration="This is Acme.")],
        pillar="onboarding",
    )
    run_generate_video(
        job,
        session,
        generate=lambda *a: (script, 7),
        critique=lambda *a: (CriticVerdict(score=0.9, safety_pass=True, notes="ok"), 2),
        tts=lambda s: base64.b64decode(_tiny_narration_b64()),
    )
    session.commit()
    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.RENDERING

    # 2) render on the real media queue: tick dispatches, worker renders, tick collects
    _drain_media_queue()
    try:
        with start_worker(celery_app, queues=[MEDIA_QUEUE], perform_ping_check=False):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                advance_video_renders(session, datetime.now(UTC))
                session.refresh(item)
                if item.status != ContentItemStatus.RENDERING:
                    break
                time.sleep(0.5)
    finally:
        _drain_media_queue()
    assert item.status == ContentItemStatus.CRITIC_PASSED
    assert item.media_ref and (tmp_path / "ws" / item.media_ref).read_bytes()[4:8] == b"ftyp"

    # 3) pace + publish through the real pass and the real adapter (fake YouTube HTTP)
    api = _FakeYouTubeApi(video_id="VID123")
    monkeypatch.setattr(
        "app.channels.youtube._build_youtube",
        lambda creds: httpx.Client(
            transport=httpx.MockTransport(api.handler),
            headers={"Authorization": f"Bearer {creds}"},
        ),
    )
    now = datetime.now(UTC)
    assert pace_content(session, now) != []
    published = publish_scheduled(session, now + timedelta(hours=1))
    session.refresh(item)
    assert [p.id for p in published] == [item.id]
    assert item.status == ContentItemStatus.PUBLISHED
    assert item.external_url == "https://www.youtube.com/watch?v=VID123"
    assert api.uploaded is not None  # the actual rendered MP4 bytes went up
    assert api.uploaded[4:8] == b"ftyp"
