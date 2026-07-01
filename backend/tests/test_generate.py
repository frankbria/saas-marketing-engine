"""S4.2: content generators (social + blog) with novelty + budget gating.

Drives the `generate` handler against a real SQLite file with the LLM work injected (stub generator
+ stub critic), so the worker wiring, novelty gathering, budget gate, and persistence are exercised
without a network call. The S4.3 critic-gate behaviors live in test_critic.py; here the critic is a
fixed passing verdict. One key-gated integration test hits the real API.
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
    JobRun,
    JobStatus,
    LifecycleState,
    Product,
    StrategyBrief,
)
from app.modules.crank import generate as gen_mod
from app.modules.crank.crank import ContentType
from app.modules.crank.generate import Generated, run_generate
from app.worker import enqueue, run_due_jobs


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


def _brand_json() -> str:
    return BrandKit(
        name="Acme",
        tone="confident",
        voice_descriptors=[VoiceDescriptor(descriptor="clear", guidance="short sentences")],
        visual_seeds=["indigo"],
    ).model_dump_json()


def _product(session, *, slug="live", budget=0, brand=True):
    p = Product(
        name="Acme",
        slug=slug,
        lifecycle_state=LifecycleState.LIVE,
        token_budget_cents_month=budget,
        brand_json=_brand_json() if brand else None,
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


def _channel(session, product_id, ctype=ChannelType.BLOG):
    c = Channel(product_id=product_id, type=ctype, enabled=True, autonomous=True)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _gen_job(session, product_id, channel_id, content_type):
    return enqueue(
        session, "generate", product_id=product_id, channel_id=channel_id, content_type=content_type
    )


def _pass_critique(product, brand_kit, content_type, gen):
    """A fixed passing verdict (cost 2) so the S4.2 tests don't invoke the real critic."""
    return CriticVerdict(score=0.9, safety_pass=True, notes="looks good"), 2


# --- persistence + metadata (AC1) --------------------------------------------------------------


def test_persists_social_item_with_pillar_metadata(session):
    p = _product(session)
    _brief(session, p.id)
    c = _channel(session, p.id, ChannelType.REDDIT)
    job = _gen_job(session, p.id, c.id, ContentType.SOCIAL.value)

    def stub(product, brief, brand_kit, content_type, recent_items):
        return Generated(body="hello world", meta={"pillar": "onboarding", "hashtags": ["#x"]}), 7

    cost = run_generate(job, session, generate=stub, critique=_pass_critique)
    session.commit()

    assert cost == 9  # 7 (generate) + 2 (critic)
    item = session.exec(select(ContentItem)).one()
    assert item.content_type == ContentType.SOCIAL.value
    assert item.status == ContentItemStatus.CRITIC_PASSED  # passed the S4.3 gate
    assert item.channel_id == c.id
    assert item.title is None
    assert item.body == "hello world"
    assert json.loads(item.meta_json)["pillar"] == "onboarding"  # AC: references a content pillar


def test_persists_blog_item_with_title_and_seo_metadata(session):
    p = _product(session)
    _brief(session, p.id)
    c = _channel(session, p.id, ChannelType.BLOG)
    job = _gen_job(session, p.id, c.id, ContentType.BLOG.value)

    def stub(product, brief, brand_kit, content_type, recent_items):
        meta = {"pillar": "automation", "slug": "how-to", "meta_description": "desc"}
        return Generated(title="How To", body="# Body", meta=meta), 30

    run_generate(job, session, generate=stub, critique=_pass_critique)
    session.commit()

    item = session.exec(select(ContentItem)).one()
    assert item.title == "How To"
    meta = json.loads(item.meta_json)
    assert meta["slug"] == "how-to" and meta["pillar"] == "automation"


# --- routing (real generator dispatches to the right client call) ------------------------------


def test_real_generate_routes_by_content_type(session, monkeypatch):
    p = _product(session)
    brief = _brief(session, p.id, pillars=["onboarding"])
    brand_kit = BrandKit.model_validate_json(p.brand_json)
    calls = {}

    def fake_social(client, name, kit, positioning, pillars, recent):
        calls["social"] = (pillars, recent)
        from app.ai.client import SocialPost

        return SocialPost(body="s", hashtags=[], pillar="onboarding"), 3

    def fake_blog(client, name, kit, positioning, pillars, recent):
        calls["blog"] = (pillars, recent)
        from app.ai.client import BlogArticle

        return (
            BlogArticle(title="t", slug="s", meta_description="m", body="b", pillar="onboarding"),
            9,
        )

    monkeypatch.setattr(gen_mod, "build_client", lambda: object())
    monkeypatch.setattr(gen_mod, "generate_social_post", fake_social)
    monkeypatch.setattr(gen_mod, "generate_blog_article", fake_blog)

    social, c_social = gen_mod._real_generate(p, brief, brand_kit, "social", ["prior"])
    blog, c_blog = gen_mod._real_generate(p, brief, brand_kit, "blog", ["prior"])

    assert (c_social, c_blog) == (3, 9)
    assert social.title is None and blog.title == "t"
    assert calls["social"][0] == ["onboarding"]  # pillars parsed from the brief
    assert calls["blog"][1] == ["prior"]  # recent items forwarded for novelty


def test_real_generate_rejects_phase_b_content_type(session):
    p = _product(session)
    brief = _brief(session, p.id)
    brand_kit = BrandKit.model_validate_json(p.brand_json)
    with pytest.raises(LookupError):
        gen_mod._real_generate(p, brief, brand_kit, "video", [])


def test_real_generate_rejects_hallucinated_pillar(session, monkeypatch):
    # The model must echo a pillar from the brief; a made-up one is rejected, not persisted.
    p = _product(session)
    brief = _brief(session, p.id, pillars=["onboarding"])
    brand_kit = BrandKit.model_validate_json(p.brand_json)

    def fake_social(client, name, kit, positioning, pillars, recent):
        from app.ai.client import SocialPost

        return SocialPost(body="s", hashtags=[], pillar="totally-made-up"), 3

    monkeypatch.setattr(gen_mod, "build_client", lambda: object())
    monkeypatch.setattr(gen_mod, "generate_social_post", fake_social)

    with pytest.raises(RuntimeError, match="unknown pillar"):
        gen_mod._real_generate(p, brief, brand_kit, "social", [])


def test_run_generate_rejects_unsupported_content_type_before_budget(session):
    # An unsupported content_type must fail as a wiring error up front — before budget math or the
    # generator runs (and independent of an API key).
    p = _product(session, budget=100)
    _brief(session, p.id)
    c = _channel(session, p.id, ChannelType.BLOG)
    job = _gen_job(session, p.id, c.id, "video")

    def stub(*a, **k):
        raise AssertionError("generator must not run for an unsupported content_type")

    with pytest.raises(LookupError, match="unsupported content_type"):
        run_generate(job, session, generate=stub)


# --- novelty (AC2) -----------------------------------------------------------------------------


def test_recent_items_fed_to_generator_newest_first(session):
    p = _product(session)
    _brief(session, p.id)
    c = _channel(session, p.id, ChannelType.BLOG)
    # Two prior items on the channel + one terminal-failure item that must be excluded.
    session.add(ContentItem(product_id=p.id, channel_id=c.id, content_type="blog", body="older"))
    session.commit()
    session.add(ContentItem(product_id=p.id, channel_id=c.id, content_type="blog", body="newer"))
    session.add(
        ContentItem(
            product_id=p.id,
            channel_id=c.id,
            content_type="blog",
            body="rejected",
            status=ContentItemStatus.GUARD_FAILED,
        )
    )
    session.commit()

    job = _gen_job(session, p.id, c.id, ContentType.BLOG.value)
    captured = {}

    def stub(product, brief, brand_kit, content_type, recent_items):
        captured["recent"] = recent_items
        return Generated(title="t", body="new", meta={"pillar": "x"}), 1

    run_generate(job, session, generate=stub, critique=_pass_critique)

    assert captured["recent"] == ["newer", "older"]  # newest first, failed item excluded


def test_recent_items_scoped_per_channel(session):
    p = _product(session)
    _brief(session, p.id)
    blog = _channel(session, p.id, ChannelType.BLOG)
    reddit = _channel(session, p.id, ChannelType.REDDIT)
    session.add(ContentItem(product_id=p.id, channel_id=reddit.id, content_type="social", body="r"))
    session.commit()

    job = _gen_job(session, p.id, blog.id, ContentType.BLOG.value)
    captured = {}

    def stub(product, brief, brand_kit, content_type, recent_items):
        captured["recent"] = recent_items
        return Generated(title="t", body="b", meta={"pillar": "x"}), 1

    run_generate(job, session, generate=stub, critique=_pass_critique)
    assert captured["recent"] == []  # the reddit item does not bleed into the blog channel


# --- budget (AC3) ------------------------------------------------------------------------------


def test_over_budget_blocks_without_generating(session):
    p = _product(session, budget=100)
    _brief(session, p.id)
    c = _channel(session, p.id, ChannelType.BLOG)
    # Prior spend already at/over the cap.
    session.add(JobRun(product_id=p.id, kind="generate", token_cost_cents=100))
    session.commit()
    job = _gen_job(session, p.id, c.id, ContentType.BLOG.value)

    def stub(*a, **k):
        raise AssertionError("generator must not run when over budget")

    with pytest.raises(RuntimeError, match="over monthly token budget"):
        run_generate(job, session, generate=stub)
    session.rollback()
    assert session.exec(select(ContentItem)).all() == []


def test_insufficient_budget_for_reservation_blocks(session):
    p = _product(session, budget=1)  # 1 cent can't cover a blog call's reserved output
    _brief(session, p.id)
    c = _channel(session, p.id, ChannelType.BLOG)
    job = _gen_job(session, p.id, c.id, ContentType.BLOG.value)

    with pytest.raises(RuntimeError, match="insufficient budget"):
        run_generate(job, session, generate=lambda *a, **k: (Generated(body="x", meta={}), 1))


def test_zero_budget_is_unlimited(session):
    p = _product(session, budget=0)
    _brief(session, p.id)
    c = _channel(session, p.id, ChannelType.BLOG)
    job = _gen_job(session, p.id, c.id, ContentType.BLOG.value)

    run_generate(
        job,
        session,
        generate=lambda *a, **k: (Generated(title="t", body="b", meta={}), 5),
        critique=_pass_critique,
    )
    session.commit()
    assert session.exec(select(ContentItem)).one().body == "b"


# --- worker wiring: cost recorded on job_run (AC3) ---------------------------------------------


def test_cost_recorded_on_job_run_via_worker(session, monkeypatch):
    p = _product(session)
    _brief(session, p.id)
    c = _channel(session, p.id, ChannelType.BLOG)
    job = _gen_job(session, p.id, c.id, ContentType.BLOG.value)

    monkeypatch.setattr(
        gen_mod,
        "_GENERATE",
        lambda *a, **k: (Generated(title="t", body="b", meta={"pillar": "x"}), 42),
    )
    monkeypatch.setattr(gen_mod, "_CRITIQUE", _pass_critique)  # cost 2
    run_due_jobs(session)

    session.refresh(job)
    assert job.status == JobStatus.DONE
    assert job.token_cost_cents == 44  # 42 (generate) + 2 (critic)
    assert session.exec(select(ContentItem)).one().body == "b"


# --- guards ------------------------------------------------------------------------------------


def test_missing_cell_identity_fails_unrecoverably(session):
    job = enqueue(session, "generate", product_id=None)  # missing channel_id/content_type
    run_due_jobs(session)
    session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.attempts == 1  # LookupError → no retry


def test_missing_brand_or_brief_fails(session):
    # brand_json present, brief missing
    p = _product(session, slug="nobrief")
    c = _channel(session, p.id, ChannelType.BLOG)
    job = _gen_job(session, p.id, c.id, ContentType.BLOG.value)
    with pytest.raises(LookupError, match="no strategy brief"):
        run_generate(job, session, generate=lambda *a, **k: (Generated(body="x", meta={}), 0))

    # brief present, brand_json missing
    p2 = _product(session, slug="nobrand", brand=False)
    _brief(session, p2.id)
    c2 = _channel(session, p2.id, ChannelType.BLOG)
    job2 = _gen_job(session, p2.id, c2.id, ContentType.BLOG.value)
    with pytest.raises(LookupError, match="no brand_json"):
        run_generate(job2, session, generate=lambda *a, **k: (Generated(body="x", meta={}), 0))


# --- integration (real API, key-gated) ---------------------------------------------------------


@pytest.mark.skipif(settings.anthropic_api_key is None, reason="requires SME_ANTHROPIC_API_KEY")
def test_integration_real_social_and_blog(session):
    p = _product(session)
    _brief(session, p.id)
    blog = _channel(session, p.id, ChannelType.BLOG)
    reddit = _channel(session, p.id, ChannelType.REDDIT)

    blog_job = _gen_job(session, p.id, blog.id, ContentType.BLOG.value)
    social_job = _gen_job(session, p.id, reddit.id, ContentType.SOCIAL.value)

    blog_cost = run_generate(blog_job, session)
    social_cost = run_generate(social_job, session)
    session.commit()

    items = {i.content_type: i for i in session.exec(select(ContentItem)).all()}
    assert items["blog"].title and items["blog"].body
    assert items["social"].body
    assert json.loads(items["blog"].meta_json)["pillar"]
    assert blog_cost > 0 and social_cost > 0
