"""S4.3: critic + safety quality gate integrated into the generate flow.

Drives `run_generate` with stub generator + stub critic (no network), exercising the gate's
decisions: pass / regenerate-then-pass / exhaust-then-skip / safety-hard-block, verdict persistence,
and budget-limited regeneration. One key-gated test hits the real critic.
"""

import json

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.ai.client import BrandKit, CriticVerdict, VoiceDescriptor
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
from app.modules.crank import generate as gen_mod
from app.modules.crank.generate import Generated, run_generate
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


def _setup(session, *, budget=0):
    brand = BrandKit(
        name="Acme",
        tone="clear",
        voice_descriptors=[VoiceDescriptor(descriptor="clear", guidance="short")],
        visual_seeds=[],
    )
    p = Product(
        name="Acme",
        slug="acme",
        lifecycle_state=LifecycleState.LIVE,
        token_budget_cents_month=budget,
        brand_json=brand.model_dump_json(),
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    session.add(
        StrategyBrief(
            product_id=p.id,
            icp_json="{}",
            pain_points_json="[]",
            positioning="pos",
            channel_plan_json="[]",
            content_pillars_json=json.dumps(["a"]),
            cadence_json="{}",
        )
    )
    c = Channel(product_id=p.id, type=ChannelType.BLOG, enabled=True, autonomous=True)
    session.add(c)
    session.commit()
    session.refresh(c)
    job = enqueue(session, "generate", product_id=p.id, channel_id=c.id, content_type="blog")
    return p, c, job


def _counting_generate():
    """A generate stub that counts calls and tags each candidate body with the attempt number."""
    calls = {"n": 0}

    def gen(product, brief, brand_kit, content_type, recent_items):
        calls["n"] += 1
        return Generated(title="t", body=f"draft {calls['n']}", meta={"pillar": "a"}), 3

    return gen, calls


def _scripted_critic(*verdicts):
    """A critic stub that returns the given verdicts in order (cost 1 each)."""
    seq = iter(verdicts)

    def critique(product, brand_kit, content_type, gen):
        return next(seq), 1

    return critique


def _v(score, safety_pass=True, notes="n"):
    return CriticVerdict(score=score, safety_pass=safety_pass, notes=notes)


def test_passing_verdict_marks_critic_passed_and_persists_scores(session):
    _p, _c, job = _setup(session)
    gen, calls = _counting_generate()

    cost = run_generate(
        job, session, generate=gen, critique=_scripted_critic(_v(0.9, notes="great"))
    )
    session.commit()

    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.CRITIC_PASSED
    assert item.critic_score == 0.9
    assert item.critic_notes == "great"  # AC4: scores/notes persisted
    assert calls["n"] == 1  # no regeneration needed
    assert cost == 4  # 3 (generate) + 1 (critic)


def test_score_at_threshold_passes(session, monkeypatch):
    monkeypatch.setattr(settings, "critic_score_threshold", 0.7)
    _p, _c, job = _setup(session)
    gen, _calls = _counting_generate()

    run_generate(job, session, generate=gen, critique=_scripted_critic(_v(0.7)))  # exactly at bar
    session.commit()
    assert session.exec(select(ContentItem)).one().status == ContentItemStatus.CRITIC_PASSED


def test_safety_fail_hard_blocks_without_regeneration(session):
    _p, _c, job = _setup(session)
    gen, calls = _counting_generate()

    # safety_pass=False even with a high score → guard_failed, and NO regeneration attempt.
    run_generate(job, session, generate=gen, critique=_scripted_critic(_v(0.95, safety_pass=False)))
    session.commit()

    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.GUARD_FAILED
    assert calls["n"] == 1  # hard block — did not regenerate
    assert item.critic_score == 0.95  # verdict still persisted


def test_low_score_regenerates_then_passes(session, monkeypatch):
    monkeypatch.setattr(settings, "critic_max_regenerations", 2)
    _p, _c, job = _setup(session)
    gen, calls = _counting_generate()

    cost = run_generate(job, session, generate=gen, critique=_scripted_critic(_v(0.4), _v(0.85)))
    session.commit()

    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.CRITIC_PASSED
    assert calls["n"] == 2  # regenerated once
    assert item.body == "draft 2"  # the accepted (second) candidate is the one persisted
    assert item.critic_score == 0.85
    assert cost == 8  # two passes: (3+1) + (3+1)


def test_low_score_exhausts_regenerations_then_skips_logs(session, monkeypatch):
    monkeypatch.setattr(settings, "critic_max_regenerations", 2)
    _p, _c, job = _setup(session)
    gen, calls = _counting_generate()

    run_generate(job, session, generate=gen, critique=_scripted_critic(_v(0.3), _v(0.4), _v(0.5)))
    session.commit()

    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.CRITIC_FAILED  # skip+log
    assert calls["n"] == 3  # 1 initial + 2 regenerations
    assert item.body == "draft 3"  # last candidate persisted with its verdict
    assert item.critic_score == 0.5


def test_only_one_content_item_row_per_cell(session, monkeypatch):
    monkeypatch.setattr(settings, "critic_max_regenerations", 2)
    _p, _c, job = _setup(session)
    gen, _calls = _counting_generate()

    run_generate(job, session, generate=gen, critique=_scripted_critic(_v(0.3), _v(0.4), _v(0.5)))
    session.commit()
    assert len(session.exec(select(ContentItem)).all()) == 1  # not one row per attempt


def test_budget_stops_regeneration_and_skips_logs(session, monkeypatch):
    monkeypatch.setattr(settings, "critic_max_regenerations", 2)
    # Reserve 10c/attempt with 11c of budget → exactly one attempt fits; a 2nd can't be afforded.
    monkeypatch.setattr(gen_mod, "_reserve_one_attempt", lambda *a, **k: 10)
    _p, _c, job = _setup(session, budget=11)
    gen, calls = _counting_generate()

    run_generate(job, session, generate=gen, critique=_scripted_critic(_v(0.2), _v(0.2), _v(0.2)))
    session.commit()

    item = session.exec(select(ContentItem)).one()
    assert calls["n"] == 1  # budget stopped further regeneration
    assert item.status == ContentItemStatus.CRITIC_FAILED  # skip+log the one low-scoring candidate


def test_over_budget_blocks_before_any_pass(session, monkeypatch):
    monkeypatch.setattr(gen_mod, "_reserve_one_attempt", lambda *a, **k: 10)
    _p, _c, job = _setup(session, budget=5)  # can't afford even one 10c pass
    gen, calls = _counting_generate()

    with pytest.raises(RuntimeError, match="insufficient budget"):
        run_generate(job, session, generate=gen, critique=_scripted_critic(_v(0.9)))
    session.rollback()
    assert calls["n"] == 0
    assert session.exec(select(ContentItem)).all() == []


def test_critic_settings_reject_out_of_range_config():
    # Bad deploy config must fail loudly at startup, not silently disable/break the gate.
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(critic_score_threshold=1.5)
    with pytest.raises(ValidationError):
        Settings(critic_score_threshold=-0.1)
    with pytest.raises(ValidationError):
        Settings(critic_max_regenerations=-1)
    with pytest.raises(ValidationError):
        Settings(critic_max_regenerations=100000)  # absurdly high → arbitrarily many LLM calls


@pytest.mark.skipif(settings.anthropic_api_key is None, reason="requires SME_ANTHROPIC_API_KEY")
def test_integration_real_critic(session):
    from app.ai.client import build_client, critique_content

    brand = BrandKit(
        name="Acme",
        tone="clear",
        voice_descriptors=[VoiceDescriptor(descriptor="clear", guidance="short")],
        visual_seeds=[],
    )
    verdict, cost = critique_content(
        build_client(),
        "Acme",
        brand,
        "social",
        None,
        "Try our tool — it saves you hours every week. Free trial, no card needed.",
    )
    assert 0.0 <= verdict.score <= 1.0
    assert isinstance(verdict.safety_pass, bool)
    assert cost > 0
