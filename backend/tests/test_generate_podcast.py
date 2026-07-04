"""S5.2: podcast generator + music-mix pipeline (issue #30).

Drives `run_generate_podcast` against a real SQLite file with the LLM/TTS work injected (stub script
generator + stub critic + stub TTS), so the worker wiring, gates, workspace checkpointing, and
persistence are exercised without a network call.

The load-bearing divergence from video: a narration-only episode (the default) finishes *in-process*
at `critic_passed` and never dispatches to the GPU `media` queue — proven here with a poisoned send
seam and an assertion that no music mix is enqueued. Only a channel that opts into a music bed
(`profile_json.music_bed`) leaves the item `rendering` for the podcast tick, whose dispatch/poll
boundary is injected here (the real queue path is covered by tests/test_media_podcast_queue.py).
"""

import base64
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.ai.client import BrandKit, CriticVerdict, PodcastScript, PodcastSegment, VoiceDescriptor
from app.config import settings
from app.models import (
    Channel,
    ChannelType,
    ContentItem,
    ContentItemStatus,
    LifecycleState,
    Product,
    StrategyBrief,
)
from app.modules.crank.crank import _CHANNEL_CONTENT_TYPES, ContentType
from app.modules.crank.generate_podcast import run_generate_podcast
from app.modules.crank.podcast_pipeline import advance_podcast_renders
from app.worker import enqueue


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


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(settings, "workspace_root", str(root))
    return root


def _brand_json() -> str:
    return BrandKit(
        name="Acme",
        tone="confident",
        voice_descriptors=[VoiceDescriptor(descriptor="clear", guidance="short sentences")],
        visual_seeds=["indigo"],
    ).model_dump_json()


def _product(session, *, slug="live", budget=0):
    p = Product(
        name="Acme",
        slug=slug,
        lifecycle_state=LifecycleState.LIVE,
        token_budget_cents_month=budget,
        brand_json=_brand_json(),
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _brief(session, product_id, *, pillars=("onboarding", "automation")):
    b = StrategyBrief(
        product_id=product_id,
        icp_json="{}",
        pain_points_json="[]",
        positioning="The fastest way to X.",
        channel_plan_json="[]",
        content_pillars_json=json.dumps(list(pillars)),
        cadence_json="{}",
    )
    session.add(b)
    session.commit()
    session.refresh(b)
    return b


def _channel(session, product_id, *, music_bed=False):
    profile = json.dumps({"music_bed": True}) if music_bed else None
    c = Channel(
        product_id=product_id,
        type=ChannelType.PODCAST,
        enabled=True,
        autonomous=True,
        profile_json=profile,
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _podcast_job(session, product_id, channel_id):
    return enqueue(
        session,
        "generate",
        product_id=product_id,
        channel_id=channel_id,
        content_type=ContentType.PODCAST.value,
    )


def _script(*, title="Why Acme wins", pillar="onboarding", music_prompt=None) -> PodcastScript:
    return PodcastScript(
        title=title,
        description="A short episode about Acme.",
        segments=[
            PodcastSegment(heading="Intro", narration="This is Acme."),
            PodcastSegment(heading="How it helps", narration="Acme ships for you."),
        ],
        pillar=pillar,
        music_prompt=music_prompt,
    )


def _stub_generate(script=None, cost=7):
    def stub(product, brief, brand_kit, recent_items):
        return (script or _script()), cost

    return stub


def _pass_critique(product, brand_kit, content_type, gen):
    return CriticVerdict(score=0.9, safety_pass=True, notes="looks good"), 2


def _stub_tts(audio=b"ID3fakemp3"):
    def stub(script):
        return audio

    return stub


def _setup(session, *, music_bed=False):
    p = _product(session)
    _brief(session, p.id)
    c = _channel(session, p.id, music_bed=music_bed)
    job = _podcast_job(session, p.id, c.id)
    return p, c, job


# --- generator: the zero-GPU default (AC1 / headline AC) -----------------------------------------


def test_no_music_episode_finalizes_without_touching_the_queue(session, workspace):
    # The load-bearing AC: a narration-only episode is publish-ready in-process and never enqueues
    # a GPU mix. The dispatch seam is poisoned to prove nothing is sent.
    p, c, job = _setup(session)

    def poisoned_send(*a):
        raise AssertionError("a narration-only episode must never dispatch a GPU mix")

    cost = run_generate_podcast(
        job, session, generate=_stub_generate(), critique=_pass_critique, tts=_stub_tts(b"AUDIO")
    )
    session.commit()

    assert cost == 9  # 7 (script gen) + 2 (critic)
    item = session.exec(select(ContentItem)).one()
    assert item.content_type == ContentType.PODCAST.value
    assert item.status == ContentItemStatus.CRITIC_PASSED  # publish-ready, no render stage
    assert item.media_ref == f"{p.slug}/media/podcast/job-{job.id}/narration.mp3"
    assert (workspace / item.media_ref).read_bytes() == b"AUDIO"  # narration IS the episode
    meta = json.loads(item.meta_json)
    assert "render" not in meta  # no GPU bookkeeping for a bedless episode
    assert "music_prompt" not in meta

    # And the tick is a no-op — no rendering items to advance.
    advance_podcast_renders(session, datetime.now(UTC), send=poisoned_send, poll=poisoned_send)


def test_music_bed_episode_awaits_the_render_tick(session, workspace):
    p, c, job = _setup(session, music_bed=True)
    run_generate_podcast(
        job,
        session,
        generate=_stub_generate(_script(music_prompt="warm lo-fi")),
        critique=_pass_critique,
        tts=_stub_tts(),
    )
    session.commit()

    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.RENDERING  # gates passed; awaiting the GPU mix
    assert item.media_ref is None  # set by the collect step, not yet
    meta = json.loads(item.meta_json)
    assert meta["music_prompt"] == "warm lo-fi"
    assert meta["render"] == {"dispatches": 0}
    assert meta["podcast_dir"] == f"{p.slug}/media/podcast/job-{job.id}"


def test_checkpoints_script_and_narration_in_workspace(session, workspace):
    p, _c, job = _setup(session, music_bed=True)
    run_generate_podcast(
        job, session, generate=_stub_generate(), critique=_pass_critique, tts=_stub_tts(b"AUDIO")
    )
    podcast_dir = workspace / p.slug / "media" / "podcast" / f"job-{job.id}"
    checkpoint = json.loads((podcast_dir / "script.json").read_text())
    assert checkpoint["script"]["title"] == "Why Acme wins"
    assert checkpoint["critic"]["score"] == 0.9
    assert (podcast_dir / "narration.mp3").read_bytes() == b"AUDIO"


def test_retry_reuses_checkpoints_without_respending(session, workspace):
    # Worker-retry resumability (AC §8.3): a second run reuses the checkpointed script + narration.
    p, c, job = _setup(session, music_bed=True)
    run_generate_podcast(
        job, session, generate=_stub_generate(), critique=_pass_critique, tts=_stub_tts()
    )
    session.rollback()  # crash-rollback: files survive, the row doesn't

    def poisoned_generate(*a):
        raise AssertionError("script must come from the checkpoint on retry")

    def poisoned_tts(*a):
        raise AssertionError("narration must come from the checkpoint on retry")

    cost = run_generate_podcast(
        job, session, generate=poisoned_generate, critique=_pass_critique, tts=poisoned_tts
    )
    session.commit()
    assert cost == 0  # nothing re-spent
    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.RENDERING


def test_resumed_no_music_episode_finalizes(session, workspace):
    # Resume must respect the channel's (no-)music decision: a bedless episode resumes straight to
    # critic_passed with its media_ref set, not to rendering.
    p, c, job = _setup(session)  # no music bed
    run_generate_podcast(
        job, session, generate=_stub_generate(), critique=_pass_critique, tts=_stub_tts(b"AUDIO")
    )
    session.rollback()

    run_generate_podcast(
        job,
        session,
        generate=lambda *a: (_ for _ in ()).throw(AssertionError("must reuse checkpoint")),
        critique=_pass_critique,
        tts=lambda *a: (_ for _ in ()).throw(AssertionError("must reuse checkpoint")),
    )
    session.commit()
    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.CRITIC_PASSED
    assert item.media_ref.endswith("narration.mp3")


# --- generator: gates (AC2) ----------------------------------------------------------------------


def test_critic_failure_skips_tts(session, workspace):
    p, _c, job = _setup(session)

    def low_critique(product, brand_kit, content_type, gen):
        return CriticVerdict(score=0.1, safety_pass=True, notes="weak"), 2

    def poisoned_tts(script):
        raise AssertionError("TTS must not run for a gate-failed script")

    run_generate_podcast(
        job, session, generate=_stub_generate(), critique=low_critique, tts=poisoned_tts
    )
    session.commit()
    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.CRITIC_FAILED
    assert not (workspace / p.slug).exists()  # no checkpoint for a rejected script


def test_safety_failure_hard_blocks(session, workspace):
    _p, _c, job = _setup(session)
    calls = []

    def unsafe_critique(product, brand_kit, content_type, gen):
        calls.append(1)
        return CriticVerdict(score=0.9, safety_pass=False, notes="unsafe"), 2

    run_generate_podcast(
        job, session, generate=_stub_generate(), critique=unsafe_critique, tts=_stub_tts()
    )
    session.commit()
    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.GUARD_FAILED
    assert len(calls) == 1  # hard block — no regeneration


def test_deterministic_guard_blocks_blocklisted_script(session, workspace):
    _p, _c, job = _setup(session)
    bad = _script(title="Guaranteed growth")  # trips the default blocklist regex

    run_generate_podcast(
        job, session, generate=_stub_generate(bad), critique=_pass_critique, tts=_stub_tts()
    )
    session.commit()
    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.GUARD_FAILED
    assert item.error


def test_low_score_regenerates_then_passes(session, workspace):
    _p, _c, job = _setup(session)
    verdicts = [
        (CriticVerdict(score=0.2, safety_pass=True, notes="weak"), 2),
        (CriticVerdict(score=0.9, safety_pass=True, notes="better"), 2),
    ]

    def critique(product, brand_kit, content_type, gen):
        return verdicts.pop(0)

    cost = run_generate_podcast(
        job, session, generate=_stub_generate(), critique=critique, tts=_stub_tts()
    )
    session.commit()
    assert cost == 18  # two generate+critic passes: (7+2) * 2
    assert session.exec(select(ContentItem)).one().status == ContentItemStatus.CRITIC_PASSED


def test_unknown_pillar_fails_loudly(session, workspace):
    _p, _c, job = _setup(session)
    off = _stub_generate(_script(pillar="hallucinated"))
    with pytest.raises(RuntimeError, match="pillar"):
        run_generate_podcast(job, session, generate=off, critique=_pass_critique, tts=_stub_tts())


def test_over_budget_raises_before_any_call(session, workspace):
    p = _product(session, budget=1)
    _brief(session, p.id)
    c = _channel(session, p.id)
    job = _podcast_job(session, p.id, c.id)

    def poisoned_generate(*a):
        raise AssertionError("no LLM call may happen when the budget can't cover one pass")

    with pytest.raises(RuntimeError, match="budget"):
        run_generate_podcast(
            job, session, generate=poisoned_generate, critique=_pass_critique, tts=_stub_tts()
        )


# --- render tick: dispatch → poll → collect (music-bed path only) --------------------------------


def _rendering_item(session, workspace, *, dispatches=0, task_id=None):
    p, c, job = _setup(session, music_bed=True)
    run_generate_podcast(
        job, session, generate=_stub_generate(), critique=_pass_critique, tts=_stub_tts()
    )
    session.commit()
    item = session.exec(select(ContentItem)).one()
    meta = json.loads(item.meta_json)
    meta["render"] = {"dispatches": dispatches, **({"task_id": task_id} if task_id else {})}
    item.meta_json = json.dumps(meta)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def test_tick_dispatches_undispatched_mix(session, workspace):
    item = _rendering_item(session, workspace)
    sent = []

    def send(narration_b64, music_prompt, max_bytes):
        sent.append((narration_b64, music_prompt, max_bytes))
        return "task-1"

    advance_podcast_renders(
        session, datetime.now(UTC), send=send, poll=lambda tid: ("pending", None)
    )
    session.refresh(item)
    meta = json.loads(item.meta_json)
    assert meta["render"]["task_id"] == "task-1"
    assert meta["render"]["dispatches"] == 1
    narration_b64, music_prompt, max_bytes = sent[0]
    assert base64.b64decode(narration_b64) == b"ID3fakemp3"  # payload from the workspace checkpoint
    assert music_prompt == meta["music_prompt"]
    assert max_bytes == settings.podcast_render_max_bytes


def test_tick_collects_finished_mix_into_workspace(session, workspace):
    item = _rendering_item(session, workspace, dispatches=1, task_id="task-1")
    mp3 = b"ID3\x00\x00\x00mixed-episode"

    def poll(task_id):
        return ("success", base64.b64encode(mp3).decode())

    advance_podcast_renders(session, datetime.now(UTC), send=lambda *a: "x", poll=poll)
    session.refresh(item)
    assert item.status == ContentItemStatus.CRITIC_PASSED
    assert item.media_ref == json.loads(item.meta_json)["podcast_dir"] + "/episode.mp3"
    assert (workspace / item.media_ref).read_bytes() == mp3


def test_tick_redispatches_failed_mix_up_to_bound(session, workspace):
    item = _rendering_item(session, workspace, dispatches=1, task_id="task-1")
    advance_podcast_renders(
        session, datetime.now(UTC), send=lambda *a: "task-2", poll=lambda tid: ("failed", "boom")
    )
    session.refresh(item)
    meta = json.loads(item.meta_json)
    assert meta["render"]["dispatches"] == 2
    assert meta["render"]["task_id"] == "task-2"
    assert item.status == ContentItemStatus.RENDERING


def test_tick_fails_item_when_dispatch_budget_exhausted(session, workspace):
    max_d = settings.podcast_max_render_dispatches
    item = _rendering_item(session, workspace, dispatches=max_d, task_id="task-N")
    advance_podcast_renders(
        session, datetime.now(UTC), send=lambda *a: "x", poll=lambda tid: ("failed", "boom")
    )
    session.refresh(item)
    assert item.status == ContentItemStatus.RENDER_FAILED
    assert "boom" in (item.error or "")


def test_tick_rejects_oversized_mix(session, workspace, monkeypatch):
    item = _rendering_item(session, workspace, dispatches=1, task_id="task-1")
    monkeypatch.setattr(settings, "podcast_render_max_bytes", 4)

    def poll(task_id):
        return ("success", base64.b64encode(b"way-too-big").decode())

    advance_podcast_renders(session, datetime.now(UTC), send=lambda *a: "task-2", poll=poll)
    session.refresh(item)
    assert item.status != ContentItemStatus.CRITIC_PASSED
    assert item.media_ref is None


def test_tick_never_raises_on_corrupt_meta(session, workspace):
    item = _rendering_item(session, workspace)
    item.meta_json = "not json"
    session.add(item)
    session.commit()
    advance_podcast_renders(
        session, datetime.now(UTC), send=lambda *a: "x", poll=lambda tid: ("pending", None)
    )


def test_tick_ignores_video_rendering_items(session, workspace):
    # Both video and podcast use `rendering`; the podcast tick must scope to podcast items only or
    # it would try to mix a video (whose meta has no podcast_dir) every tick.
    from app.modules.crank.generate_video import run_generate_video

    p = _product(session, slug="vid")
    _brief(session, p.id)
    vc = Channel(product_id=p.id, type=ChannelType.YOUTUBE, enabled=True, autonomous=True)
    session.add(vc)
    session.commit()
    session.refresh(vc)
    vjob = enqueue(
        session,
        "generate",
        product_id=p.id,
        channel_id=vc.id,
        content_type=ContentType.VIDEO.value,
    )

    from app.ai.client import VideoScript, VideoSegment

    def vgen(product, brief, brand_kit, recent):
        return (
            VideoScript(
                title="V",
                description="d",
                segments=[VideoSegment(caption="c", narration="n")],
                pillar="onboarding",
            ),
            5,
        )

    run_generate_video(vjob, session, generate=vgen, critique=_pass_critique, tts=_stub_tts())
    session.commit()

    def poisoned_send(*a):
        raise AssertionError("the podcast tick must not dispatch a video render")

    advance_podcast_renders(
        session, datetime.now(UTC), send=poisoned_send, poll=lambda tid: ("pending", None)
    )
    video_item = session.exec(
        select(ContentItem).where(ContentItem.content_type == ContentType.VIDEO.value)
    ).one()
    assert video_item.status == ContentItemStatus.RENDERING  # untouched by the podcast tick


# --- wiring: crank fan-out + worker routing + scheduler ------------------------------------------


def test_crank_fans_out_podcast_for_podcast_channel():
    assert _CHANNEL_CONTENT_TYPES[ChannelType.PODCAST] == (ContentType.PODCAST,)


def test_worker_routes_podcast_job_through_podcast_generator(session, workspace, monkeypatch):
    from app.modules.crank import generate_podcast as gp
    from app.worker import run_due_jobs

    _p, _c, _job = _setup(session)
    monkeypatch.setattr(gp, "_GENERATE", _stub_generate())
    monkeypatch.setattr(gp, "_CRITIQUE", _pass_critique)
    monkeypatch.setattr(gp, "_TTS", _stub_tts())
    session.commit()

    run_due_jobs(session)

    item = session.exec(select(ContentItem)).one()
    assert item.content_type == ContentType.PODCAST.value
    assert item.status == ContentItemStatus.CRITIC_PASSED  # no-music default


def test_scheduler_registers_podcast_render_tick():
    from app.scheduler import create_scheduler

    scheduler = create_scheduler()
    assert scheduler.get_job("podcast_render") is not None


def test_podcast_render_tick_advances_renders(monkeypatch):
    import app.scheduler as sched

    calls = []
    monkeypatch.setattr(sched, "advance_podcast_renders", lambda s, now: calls.append(now))
    sched._podcast_render_tick()
    assert len(calls) == 1


# --- the real TTS boundary (faked provider HTTP, per the issue's test plan) ----------------------


def test_real_tts_posts_narration_to_elevenlabs(monkeypatch):
    from pydantic import SecretStr

    from app.modules.crank.generate_podcast import _real_tts

    monkeypatch.setattr(settings, "elevenlabs_api_key", SecretStr("el-key"))
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "voice-1")
    seen = {}

    def fake_post(url, *, headers, json, timeout):
        seen.update(url=url, headers=headers, json=json)

        class _Resp:
            content = b"AUDIO"

            def raise_for_status(self):
                return None

        return _Resp()

    monkeypatch.setattr("app.modules.crank.generate_podcast.httpx.post", fake_post)
    audio = _real_tts(_script())

    assert audio == b"AUDIO"
    assert "voice-1" in seen["url"]
    assert seen["headers"]["xi-api-key"] == "el-key"
    # The narration is the segments' spoken lines, in order — headings never reach the voice.
    assert seen["json"]["text"] == "This is Acme. Acme ships for you."


def test_real_tts_fails_loudly_without_key(monkeypatch):
    from app.modules.crank.generate_podcast import _real_tts

    monkeypatch.setattr(settings, "elevenlabs_api_key", None)
    with pytest.raises(RuntimeError, match="ELEVENLABS"):
        _real_tts(_script())


# --- integration (real API, key-gated) -----------------------------------------------------------


@pytest.mark.skipif(settings.anthropic_api_key is None, reason="requires SME_ANTHROPIC_API_KEY")
def test_integration_real_podcast_script(session, workspace):
    _p, _c, job = _setup(session)
    cost = run_generate_podcast(job, session, tts=_stub_tts())
    session.commit()

    item = session.exec(select(ContentItem)).one()
    assert item.title and item.body
    assert json.loads(item.meta_json)["pillar"]
    assert cost > 0
