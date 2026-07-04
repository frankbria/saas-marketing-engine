"""S5.2: podcast music mix on the real `media` queue + full pipeline integration (issue #30).

The cold-start AC rides the S5.0 machinery exactly like video: a `media.render_audio` task enqueued
with NO worker up must park on the broker (nothing blocks, nothing is lost) and complete once a
worker joins — how the ephemeral GPU pod appears after the provisioner boots it. Real Redis + a real
Celery worker (repo policy: no mocking of Redis/Celery); skips when no broker is reachable — CI
provides one. The mix needs ffmpeg on the host, so that gates the test too.

ACE-Step (the on-pod music model) is deferred infra, so the music-bed generator is monkeypatched to
a real ffmpeg tone — the issue's prescribed "faked ACE-Step step". The worker runs in-process
(`start_worker` threads), so patching the module seam reaches the worker. Everything else — DB,
workspace, broker, worker, ffmpeg mix, and the owned RSS publish — is real.

The end-to-end test (generate → gates → tick dispatch → real queue mix → collect → pace → publish to
the owned feed) exercises every seam of the music-bed path with only the LLM/TTS callables stubbed.
"""

import base64
import json
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta

import pytest
import redis
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.ai.client import BrandKit, CriticVerdict, PodcastScript, PodcastSegment, VoiceDescriptor
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
from app.modules.crank.generate_podcast import run_generate_podcast
from app.modules.crank.podcast_pipeline import advance_podcast_renders
from app.modules.crank.publish import pace_content, publish_scheduled
from app.modules.media.queue import media_queue_depth
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


def _tone_mp3(frequency: int, seconds: float) -> bytes:
    out = subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration={seconds}",
            "-q:a",
            "9",
            "-f",
            "mp3",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return out.stdout


def _tiny_narration_b64(seconds: float = 1.0) -> str:
    return base64.b64encode(_tone_mp3(440, seconds)).decode()


def _fake_bed(prompt: str, duration_seconds: float) -> bytes:
    """Stand-in ACE-Step bed (a real tone) — the issue's prescribed faked GPU music step."""
    return _tone_mp3(220, duration_seconds)


def _is_mp3(data: bytes) -> bool:
    return len(data) > 4 and (data[:3] == b"ID3" or (data[0] == 0xFF and data[1] & 0xE0 == 0xE0))


@requires_redis
@requires_ffmpeg
def test_mix_enqueued_with_no_worker_completes_once_worker_joins(monkeypatch):
    # Cold-start AC (issue #30): the ACE-Step mix must tolerate the S5.0 provisioner gap — a job
    # enqueued while no GPU worker exists parks on the broker and completes when one boots.
    from celery.contrib.testing.worker import start_worker

    from app.modules.media.tasks import render_audio

    monkeypatch.setattr("app.modules.media.audio._real_generate_music", _fake_bed)
    _drain_media_queue()
    try:
        result = render_audio.delay(
            _tiny_narration_b64(), "warm lo-fi bed", max_bytes=100 * 1024**2
        )
        assert media_queue_depth() == 1  # parked, no worker yet — and nothing raised

        with start_worker(celery_app, queues=[MEDIA_QUEUE], perform_ping_check=False):
            mp3 = base64.b64decode(result.get(timeout=60))
        assert _is_mp3(mp3)  # a real mixed MP3 came back through the result backend
        assert media_queue_depth() == 0
    finally:
        _drain_media_queue()


# --- full pipeline: generate → gates → queue mix → collect → pace → publish (owned RSS) ----------


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
def test_full_podcast_pipeline_generate_to_publish(session, tmp_path, monkeypatch):
    # Every seam of the music-bed path end-to-end with only the LLM/TTS boundaries stubbed: the DB,
    # workspace, Redis broker, Celery worker, ffmpeg mix, and the owned RSS publish are all real.
    from celery.contrib.testing.worker import start_worker

    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "ws"))
    monkeypatch.setattr("app.modules.media.audio._real_generate_music", _fake_bed)

    product = Product(
        name="Acme",
        slug="live",
        marketing_domain="acme.example",
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
        type=ChannelType.PODCAST,
        enabled=True,
        autonomous=True,
        connect_state=ConnectState.CONNECTED,
        profile_json=json.dumps({"music_bed": True}),  # opt into the GPU mix path
    )
    session.add(channel)
    session.commit()
    session.refresh(channel)

    # 1) generate: script (stub LLM) → gates → TTS (real MP3) → `rendering` + workspace checkpoints
    job = enqueue(
        session,
        "generate",
        product_id=product.id,
        channel_id=channel.id,
        content_type=ContentType.PODCAST.value,
    )
    script = PodcastScript(
        title="Why Acme wins",
        description="A quick episode.",
        segments=[PodcastSegment(heading="Intro", narration="This is Acme.")],
        pillar="onboarding",
        music_prompt="warm lo-fi bed",
    )
    run_generate_podcast(
        job,
        session,
        generate=lambda *a: (script, 7),
        critique=lambda *a: (CriticVerdict(score=0.9, safety_pass=True, notes="ok"), 2),
        tts=lambda s: base64.b64decode(_tiny_narration_b64()),
    )
    session.commit()
    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.RENDERING  # music bed → awaits the GPU mix

    # 2) mix on the real media queue: tick dispatches, worker mixes, tick collects
    _drain_media_queue()
    try:
        with start_worker(celery_app, queues=[MEDIA_QUEUE], perform_ping_check=False):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                advance_podcast_renders(session, datetime.now(UTC))
                session.refresh(item)
                if item.status != ContentItemStatus.RENDERING:
                    break
                time.sleep(0.5)
    finally:
        _drain_media_queue()
    assert item.status == ContentItemStatus.CRITIC_PASSED
    assert item.media_ref and _is_mp3((tmp_path / "ws" / item.media_ref).read_bytes())

    # 3) pace + publish through the real pass and the real owned-feed adapter (no external creds)
    now = datetime.now(UTC)
    assert pace_content(session, now) != []
    published = publish_scheduled(session, now + timedelta(hours=1))
    session.refresh(item)
    assert [p.id for p in published] == [item.id]
    assert item.status == ContentItemStatus.PUBLISHED
    assert item.external_url.endswith(".html")

    # The feed and the episode audio landed in the product's static site tree.
    feed = tmp_path / "ws" / "live" / "site" / "podcast" / "feed.xml"
    assert feed.exists() and "Acme Podcast" in feed.read_text()
    assert list((tmp_path / "ws" / "live" / "site" / "podcast").glob("*.mp3"))
