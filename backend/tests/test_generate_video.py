"""S5.1: video generator + render pipeline (issue #29).

Drives `run_generate_video` against a real SQLite file with the LLM/TTS work injected (stub script
generator + stub critic + stub TTS), so the worker wiring, gates, workspace checkpointing, and
persistence are exercised without a network call. The GPU render itself rides the Celery `media`
queue — here the dispatch/poll boundary is injected into `advance_video_renders`, and the real
queue path is covered by tests/test_media_video_queue.py.

Resumability contract (AC): every CPU/API step checkpoints its output in the per-product workspace
and skips recomputation when the artifact already exists, so a worker retry after a crash (or a
render re-dispatch after GPU teardown) never re-spends LLM/TTS calls and never double-creates rows.
"""

import base64
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.ai.client import BrandKit, CriticVerdict, VideoScript, VideoSegment, VoiceDescriptor
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
from app.modules.crank.generate_video import run_generate_video
from app.modules.crank.video_pipeline import advance_video_renders
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
    """Isolated workspace root so artifact checkpoints land under tmp_path."""
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


def _channel(session, product_id, ctype=ChannelType.YOUTUBE):
    c = Channel(product_id=product_id, type=ctype, enabled=True, autonomous=True)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _video_job(session, product_id, channel_id):
    return enqueue(
        session,
        "generate",
        product_id=product_id,
        channel_id=channel_id,
        content_type=ContentType.VIDEO.value,
    )


def _script(*, title="Why Acme wins", pillar="onboarding") -> VideoScript:
    return VideoScript(
        title=title,
        description="A 30-second tour of Acme.",
        segments=[
            VideoSegment(caption="Meet Acme", narration="This is Acme."),
            VideoSegment(caption="Ship faster", narration="Acme ships for you."),
        ],
        pillar=pillar,
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


def _setup(session):
    p = _product(session)
    _brief(session, p.id)
    c = _channel(session, p.id)
    job = _video_job(session, p.id, c.id)
    return p, c, job


# --- generator: persistence + artifacts (AC1) ---------------------------------------------------


def test_produces_video_item_in_rendering_state(session, workspace):
    p, c, job = _setup(session)
    cost = run_generate_video(
        job, session, generate=_stub_generate(), critique=_pass_critique, tts=_stub_tts()
    )
    session.commit()

    assert cost == 9  # 7 (script gen) + 2 (critic)
    item = session.exec(select(ContentItem)).one()
    assert item.content_type == ContentType.VIDEO.value
    assert item.status == ContentItemStatus.RENDERING  # gates passed; awaiting the GPU render
    assert item.title == "Why Acme wins"
    assert "This is Acme." in item.body  # the narration is the item's reviewable substance
    meta = json.loads(item.meta_json)
    assert meta["pillar"] == "onboarding"
    assert meta["video_dir"] == f"{p.slug}/media/video/job-{job.id}"
    assert meta["render"] == {"dispatches": 0}  # the tick owns dispatch, not the handler


def test_checkpoints_script_and_narration_in_workspace(session, workspace):
    p, _c, job = _setup(session)
    run_generate_video(
        job, session, generate=_stub_generate(), critique=_pass_critique, tts=_stub_tts(b"AUDIO")
    )
    video_dir = workspace / p.slug / "media" / "video" / f"job-{job.id}"
    checkpoint = json.loads((video_dir / "script.json").read_text())
    assert checkpoint["script"]["title"] == "Why Acme wins"
    assert checkpoint["critic"]["score"] == 0.9  # the verdict rides along for crash-resume
    assert (video_dir / "narration.mp3").read_bytes() == b"AUDIO"


def test_retry_reuses_checkpoints_without_respending(session, workspace):
    # Worker-retry resumability (AC): a second run (e.g. crash after TTS, before commit) must
    # reuse the checkpointed script + narration — never re-call the LLM or TTS.
    p, c, job = _setup(session)
    run_generate_video(
        job, session, generate=_stub_generate(), critique=_pass_critique, tts=_stub_tts()
    )
    session.rollback()  # simulate the worker's crash-rollback: files survive, the row doesn't

    def poisoned_generate(*a):
        raise AssertionError("script must come from the checkpoint on retry")

    def poisoned_critique(*a):
        raise AssertionError("gates already passed for the checkpointed script")

    def poisoned_tts(*a):
        raise AssertionError("narration must come from the checkpoint on retry")

    cost = run_generate_video(
        job, session, generate=poisoned_generate, critique=poisoned_critique, tts=poisoned_tts
    )
    session.commit()
    assert cost == 0  # nothing re-spent
    item = session.exec(select(ContentItem)).one()  # and exactly one row
    assert item.status == ContentItemStatus.RENDERING


# --- generator: gates (AC2) ----------------------------------------------------------------------


def test_critic_failure_skips_tts_and_render(session, workspace):
    p, _c, job = _setup(session)

    def low_critique(product, brand_kit, content_type, gen):
        return CriticVerdict(score=0.1, safety_pass=True, notes="weak"), 2

    def poisoned_tts(script):
        raise AssertionError("TTS must not run for a gate-failed script")

    run_generate_video(
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

    run_generate_video(
        job, session, generate=_stub_generate(), critique=unsafe_critique, tts=_stub_tts()
    )
    session.commit()
    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.GUARD_FAILED
    assert len(calls) == 1  # hard block — no regeneration


def test_deterministic_guard_blocks_blocklisted_script(session, workspace):
    _p, _c, job = _setup(session)
    bad = _script(title="Guaranteed growth")  # trips the default blocklist regex

    run_generate_video(
        job, session, generate=_stub_generate(bad), critique=_pass_critique, tts=_stub_tts()
    )
    session.commit()
    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.GUARD_FAILED
    assert item.error  # the guard's reason is persisted for the operator


def test_low_score_regenerates_then_passes(session, workspace):
    _p, _c, job = _setup(session)
    verdicts = [
        (CriticVerdict(score=0.2, safety_pass=True, notes="weak"), 2),
        (CriticVerdict(score=0.9, safety_pass=True, notes="better"), 2),
    ]

    def critique(product, brand_kit, content_type, gen):
        return verdicts.pop(0)

    cost = run_generate_video(
        job, session, generate=_stub_generate(), critique=critique, tts=_stub_tts()
    )
    session.commit()
    assert cost == 18  # two generate+critic passes: (7+2) * 2
    assert session.exec(select(ContentItem)).one().status == ContentItemStatus.RENDERING


def test_unknown_pillar_fails_loudly(session, workspace):
    _p, _c, job = _setup(session)
    off_brief = _stub_generate(_script(pillar="hallucinated"))
    with pytest.raises(RuntimeError, match="pillar"):
        run_generate_video(
            job, session, generate=off_brief, critique=_pass_critique, tts=_stub_tts()
        )


def test_over_budget_raises_before_any_call(session, workspace):
    p = _product(session, budget=1)
    _brief(session, p.id)
    c = _channel(session, p.id)
    job = _video_job(session, p.id, c.id)

    def poisoned_generate(*a):
        raise AssertionError("no LLM call may happen when the budget can't cover one pass")

    with pytest.raises(RuntimeError, match="budget"):
        run_generate_video(
            job, session, generate=poisoned_generate, critique=_pass_critique, tts=_stub_tts()
        )


# --- render tick: dispatch → poll → collect (AC4/AC5) --------------------------------------------


def _rendering_item(session, workspace, *, dispatches=0, task_id=None):
    p, c, job = _setup(session)
    run_generate_video(
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


def test_tick_dispatches_undispatched_render(session, workspace):
    item = _rendering_item(session, workspace)
    sent = []

    def send(script, narration_b64, max_bytes):
        sent.append((script, narration_b64, max_bytes))
        return "task-1"

    advance_video_renders(session, datetime.now(UTC), send=send, poll=lambda tid: ("pending", None))
    session.refresh(item)
    meta = json.loads(item.meta_json)
    assert meta["render"]["task_id"] == "task-1"
    assert meta["render"]["dispatches"] == 1
    script, narration_b64, max_bytes = sent[0]
    assert script["title"] == "Why Acme wins"  # payload comes from the workspace checkpoint
    assert base64.b64decode(narration_b64) == b"ID3fakemp3"
    assert max_bytes == settings.video_render_max_bytes


def test_tick_collects_finished_render_into_workspace(session, workspace):
    item = _rendering_item(session, workspace, dispatches=1, task_id="task-1")
    mp4 = b"\x00\x00\x00\x18ftypmp42-fake-payload"

    def poll(task_id):
        return ("success", base64.b64encode(mp4).decode())

    advance_video_renders(session, datetime.now(UTC), send=lambda *a: "x", poll=poll)
    session.refresh(item)
    assert item.status == ContentItemStatus.CRITIC_PASSED  # ready for the S4.5 pace/publish pass
    assert item.media_ref == json.loads(item.meta_json)["video_dir"] + "/final.mp4"
    assert (workspace / item.media_ref).read_bytes() == mp4


def test_tick_redispatches_failed_render_up_to_bound(session, workspace):
    item = _rendering_item(session, workspace, dispatches=1, task_id="task-1")

    advance_video_renders(
        session, datetime.now(UTC), send=lambda *a: "task-2", poll=lambda tid: ("failed", "boom")
    )
    session.refresh(item)
    meta = json.loads(item.meta_json)
    assert meta["render"]["dispatches"] == 2  # failure cleared the task id; re-dispatched same tick
    assert meta["render"]["task_id"] == "task-2"
    assert item.status == ContentItemStatus.RENDERING


def test_tick_fails_item_when_dispatch_budget_exhausted(session, workspace):
    max_d = settings.video_max_render_dispatches
    item = _rendering_item(session, workspace, dispatches=max_d, task_id="task-N")

    advance_video_renders(
        session, datetime.now(UTC), send=lambda *a: "x", poll=lambda tid: ("failed", "boom")
    )
    session.refresh(item)
    assert item.status == ContentItemStatus.RENDER_FAILED  # terminal — never strands in `rendering`
    assert "boom" in (item.error or "")


def test_tick_rejects_oversized_render(session, workspace, monkeypatch):
    # Defense in depth: the task guards output size on the pod, but the collect side must also
    # refuse a result that would blow the workspace/broker budget (a lying/old worker).
    item = _rendering_item(session, workspace, dispatches=1, task_id="task-1")
    monkeypatch.setattr(settings, "video_render_max_bytes", 4)

    def poll(task_id):
        return ("success", base64.b64encode(b"way-too-big").decode())

    advance_video_renders(session, datetime.now(UTC), send=lambda *a: "task-2", poll=poll)
    session.refresh(item)
    assert item.status != ContentItemStatus.CRITIC_PASSED
    assert item.media_ref is None


def test_tick_never_raises_on_corrupt_meta(session, workspace):
    item = _rendering_item(session, workspace)
    item.meta_json = "not json"
    session.add(item)
    session.commit()
    advance_video_renders(  # must swallow + continue, not crash the scheduler tick
        session, datetime.now(UTC), send=lambda *a: "x", poll=lambda tid: ("pending", None)
    )


def test_tick_leaves_pending_render_alone(session, workspace):
    item = _rendering_item(session, workspace, dispatches=1, task_id="task-1")
    advance_video_renders(
        session, datetime.now(UTC), send=lambda *a: "x", poll=lambda tid: ("pending", None)
    )
    session.refresh(item)
    assert item.status == ContentItemStatus.RENDERING
    assert json.loads(item.meta_json)["render"]["dispatches"] == 1  # no double-dispatch


# --- wiring: crank fan-out + worker routing + scheduler (AC6) -------------------------------------


def test_crank_fans_out_video_for_youtube():
    assert _CHANNEL_CONTENT_TYPES[ChannelType.YOUTUBE] == (ContentType.VIDEO,)


def test_worker_routes_video_job_through_video_generator(session, workspace, monkeypatch):
    # The full worker path: an enqueued video job must reach run_generate_video via the
    # `generate` handler's routing (not the text path, which would reject content_type=video).
    from app.modules.crank import generate_video as gv
    from app.worker import run_due_jobs

    p, c, job = _setup(session)
    monkeypatch.setattr(gv, "_GENERATE", _stub_generate())
    monkeypatch.setattr(gv, "_CRITIQUE", _pass_critique)
    monkeypatch.setattr(gv, "_TTS", _stub_tts())
    session.commit()

    run_due_jobs(session)

    item = session.exec(select(ContentItem)).one()
    assert item.content_type == ContentType.VIDEO.value
    assert item.status == ContentItemStatus.RENDERING


def test_scheduler_registers_video_render_tick():
    from app.scheduler import create_scheduler

    scheduler = create_scheduler()
    assert scheduler.get_job("video_render") is not None


def test_video_render_tick_advances_renders(monkeypatch):
    # The tick body itself: proves the scheduler job actually drives advance_video_renders
    # (a registered-but-inert job would strand every rendering item).
    import app.scheduler as sched

    calls = []
    monkeypatch.setattr(sched, "advance_video_renders", lambda s, now: calls.append(now))
    sched._video_render_tick()
    assert len(calls) == 1


# --- the real TTS boundary (faked provider HTTP, per the issue's test plan) ----------------------


def test_real_tts_posts_script_narration_to_elevenlabs(monkeypatch):
    from pydantic import SecretStr

    from app.modules.crank.generate_video import _real_tts

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

    monkeypatch.setattr("app.modules.crank.generate_video.httpx.post", fake_post)
    audio = _real_tts(_script())

    assert audio == b"AUDIO"
    assert "voice-1" in seen["url"]
    assert seen["headers"]["xi-api-key"] == "el-key"
    # The narration is the segments' spoken lines, in order — captions never reach the voice.
    assert seen["json"]["text"] == "This is Acme. Acme ships for you."


def test_real_tts_fails_loudly_without_key(monkeypatch):
    from app.modules.crank.generate_video import _real_tts

    monkeypatch.setattr(settings, "elevenlabs_api_key", None)
    with pytest.raises(RuntimeError, match="ELEVENLABS"):
        _real_tts(_script())


# --- integration (real API, key-gated) ------------------------------------------------------------


@pytest.mark.skipif(settings.anthropic_api_key is None, reason="requires SME_ANTHROPIC_API_KEY")
def test_integration_real_video_script(session, workspace):
    # Real generate_video_script + critic against the API (mirrors test_generate.py's key-gated
    # test); TTS stays stubbed — ElevenLabs spend isn't justified for a schema check.
    p, _c, job = _setup(session)
    cost = run_generate_video(job, session, tts=_stub_tts())
    session.commit()

    item = session.exec(select(ContentItem)).one()
    assert item.title and item.body
    assert json.loads(item.meta_json)["pillar"]
    assert cost > 0
