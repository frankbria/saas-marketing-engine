"""S4.4: deterministic (non-LLM) guard — blocklist/regex + numeric claim-trace.

Unit-tests the pure `check_content` guard, its wiring into `run_generate` (a critic-passing item
that trips the guard is hard-blocked as `guard_failed`, independent of the LLM critic), and the
config validation of `guard_blocklist`.
"""

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.ai.client import BrandKit, CriticVerdict, VoiceDescriptor
from app.config import Settings, settings
from app.models import (
    Channel,
    ChannelType,
    ContentItem,
    ContentItemStatus,
    LifecycleState,
    Product,
    StrategyBrief,
)
from app.modules.crank.generate import Generated, run_generate
from app.modules.crank.guard import check_content
from app.worker import enqueue


def _brief(product_id=1, **over):
    kw = dict(
        product_id=product_id,
        icp_json="{}",
        pain_points_json="[]",
        positioning="The fastest way to ship.",
        channel_plan_json="[]",
        content_pillars_json=json.dumps(["a"]),
        cadence_json="{}",
    )
    kw.update(over)
    return StrategyBrief(**kw)


def _product(**over):
    kw = dict(name="Acme", slug="acme", lifecycle_state=LifecycleState.LIVE)
    kw.update(over)
    return Product(**kw)


# ---- unit: check_content ---------------------------------------------------


def test_clean_content_passes():
    clean = "A helpful, on-brand post about shipping fast."
    assert check_content(None, clean, _brief(), _product()) is None


def test_blocklisted_term_blocks():
    reason = check_content(None, "Our product is 100% risk-free, guaranteed!", _brief(), _product())
    assert reason is not None
    assert "block" in reason.lower()


def test_blocklist_checks_title_too():
    reason = check_content("A miracle cure", "Perfectly fine body.", _brief(), _product())
    assert reason is not None


def test_untraceable_numeric_claim_blocks():
    # "50%" is a factual claim with no support anywhere in the brief/product → hard block.
    reason = check_content(None, "Get 50% faster results with our tool.", _brief(), _product())
    assert reason is not None
    assert "50" in reason


def test_claim_traceable_to_brief_passes():
    brief = _brief(positioning="Cuts onboarding time by 50% for new teams.")
    assert check_content(None, "Users see 50% faster onboarding.", brief, _product()) is None


def test_price_claim_traces_to_product():
    prod = _product(price_amount_cents=9900)  # $99.00
    assert check_content(None, "All this for just $99 a month.", _brief(), prod) is None


def test_untraceable_price_blocks():
    prod = _product(price_amount_cents=9900)  # $99, but the copy says $19
    reason = check_content(None, "A steal at $19/mo.", _brief(), prod)
    assert reason is not None


def test_price_number_does_not_vouch_for_a_percent_claim():
    # A $99 price puts "99" in the money facts — it must NOT let a "99%" claim trace (cross-type).
    prod = _product(price_amount_cents=9900)
    reason = check_content(None, "We deliver 99% uptime.", _brief(), prod)
    assert reason is not None and "99" in reason


def test_multiplier_claim_untraceable_blocks():
    reason = check_content(None, "Be 10x more productive.", _brief(), _product())
    assert reason is not None


def test_small_numbers_are_not_claims():
    # "5 tips" / "3 steps" are not statistical claims — must not be flagged.
    assert check_content(None, "5 tips and 3 steps to ship faster.", _brief(), _product()) is None


def test_large_count_traces_to_brand_facts():
    prod = _product(brand_json='{"note": "trusted by 10,000 teams"}')
    assert check_content(None, "Join 10,000 teams already shipping.", _brief(), prod) is None


# ---- config validation -----------------------------------------------------


def test_bad_blocklist_regex_fails_loudly():
    with pytest.raises(ValidationError):
        Settings(guard_blocklist="(unclosed")


def test_blocklist_accepts_csv_env_form():
    s = Settings(guard_blocklist="foo, bar")
    assert s.guard_blocklist == ["foo", "bar"]


# ---- integration: run_generate wiring --------------------------------------


@pytest.fixture
def session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _setup(session):
    brand = BrandKit(
        name="Acme",
        tone="clear",
        voice_descriptors=[VoiceDescriptor(descriptor="clear", guidance="short")],
        visual_seeds=[],
    )
    p = _product(brand_json=brand.model_dump_json())
    session.add(p)
    session.commit()
    session.refresh(p)
    session.add(_brief(product_id=p.id))
    c = Channel(product_id=p.id, type=ChannelType.BLOG, enabled=True, autonomous=True)
    session.add(c)
    session.commit()
    session.refresh(c)
    job = enqueue(session, "generate", product_id=p.id, channel_id=c.id, content_type="blog")
    return p, c, job


def _gen_returning(body):
    def gen(product, brief, brand_kit, content_type, recent_items):
        return Generated(title="t", body=body, meta={"pillar": "a"}), 3

    return gen


def _critic(*verdicts):
    seq = iter(verdicts)

    def critique(product, brand_kit, content_type, gen):
        return next(seq), 1

    return critique


def _v(score, safety_pass=True):
    return CriticVerdict(score=score, safety_pass=safety_pass, notes="n")


def test_guard_hard_blocks_a_critic_passing_item(session):
    _p, _c, job = _setup(session)
    # Critic loves it, but it makes an untraceable "90%" claim → deterministic guard blocks it.
    run_generate(
        job,
        session,
        generate=_gen_returning("Boost conversions by 90% overnight."),
        critique=_critic(_v(0.95)),
    )
    session.commit()

    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.GUARD_FAILED  # independent of the critic
    assert item.error is not None and "90" in item.error


def test_guard_does_not_regenerate(session, monkeypatch):
    monkeypatch.setattr(settings, "critic_max_regenerations", 2)
    _p, _c, job = _setup(session)
    calls = {"n": 0}

    def gen(product, brief, brand_kit, content_type, recent_items):
        calls["n"] += 1
        return Generated(title="t", body="Grow 200% guaranteed.", meta={"pillar": "a"}), 3

    run_generate(job, session, generate=gen, critique=_critic(_v(0.95)))
    session.commit()
    assert calls["n"] == 1  # hard block, like safety fail — no regeneration


def test_clean_item_still_reaches_critic_passed(session):
    _p, _c, job = _setup(session)
    run_generate(
        job,
        session,
        generate=_gen_returning("A genuinely useful, claim-free post about shipping."),
        critique=_critic(_v(0.9)),
    )
    session.commit()

    item = session.exec(select(ContentItem)).one()
    assert item.status == ContentItemStatus.CRITIC_PASSED
    assert item.error is None
