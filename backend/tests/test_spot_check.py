"""S4.9: async spot-check sampling.

The first item per channel + a random 10% are flagged `spot_check=true` for async human review.
Flagging is an annotation set once at creation — it never changes `status`, so it can't block
publishing. Drives `run_generate` with the LLM work stubbed and the sampler injected
(deterministic), plus the review-queue API. No network.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.ai.client import BrandKit, CriticVerdict, VoiceDescriptor
from app.db import get_session
from app.main import create_app
from app.models import (
    Channel,
    ChannelType,
    ContentItem,
    ContentItemStatus,
    LifecycleState,
    Product,
    StrategyBrief,
)
from app.modules.crank.crank import ContentType
from app.modules.crank.generate import Generated, run_generate
from app.worker import enqueue


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "test.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _brand_json() -> str:
    return BrandKit(
        name="Acme",
        tone="confident",
        voice_descriptors=[VoiceDescriptor(descriptor="clear", guidance="short sentences")],
        visual_seeds=["indigo"],
    ).model_dump_json()


def _product(session, *, slug="live"):
    p = Product(
        name="Acme",
        slug=slug,
        lifecycle_state=LifecycleState.LIVE,
        token_budget_cents_month=0,
        brand_json=_brand_json(),
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _brief(session, product_id):
    b = StrategyBrief(
        product_id=product_id,
        icp_json="{}",
        pain_points_json="[]",
        positioning="The fastest way to X.",
        channel_plan_json="[]",
        content_pillars_json=json.dumps(["onboarding"]),
        cadence_json="{}",
    )
    session.add(b)
    session.commit()
    return b


def _channel(session, product_id, ctype=ChannelType.REDDIT):
    c = Channel(product_id=product_id, type=ctype, enabled=True, autonomous=True)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _gen_job(session, product_id, channel_id):
    return enqueue(
        session,
        "generate",
        product_id=product_id,
        channel_id=channel_id,
        content_type=ContentType.SOCIAL.value,
    )


def _stub_gen(product, brief, brand_kit, content_type, recent_items):
    return Generated(body="hello world", meta={"pillar": "onboarding", "hashtags": ["#x"]}), 7


def _pass_critique(product, brand_kit, content_type, gen):
    return CriticVerdict(score=0.9, safety_pass=True, notes="ok"), 2


def _run(session, product_id, channel_id, *, sample):
    job = _gen_job(session, product_id, channel_id)
    run_generate(job, session, generate=_stub_gen, critique=_pass_critique, sample=sample)
    session.commit()


# --- sampling (AC1) ----------------------------------------------------------------------------


def test_first_item_per_channel_is_always_flagged(session):
    """First item on a channel is flagged even when the random draw would not pick it."""
    p = _product(session)
    _brief(session, p.id)
    c = _channel(session, p.id)

    _run(session, p.id, c.id, sample=lambda: 0.99)  # draw well above the 10% rate

    item = session.exec(select(ContentItem)).one()
    assert item.spot_check is True


def test_later_items_flagged_only_by_random_sample(session):
    """After the first, items are flagged iff the draw falls under the 10% rate."""
    p = _product(session)
    _brief(session, p.id)
    c = _channel(session, p.id)

    _run(session, p.id, c.id, sample=lambda: 0.99)  # first — flagged regardless
    _run(session, p.id, c.id, sample=lambda: 0.99)  # draw above rate → not flagged
    _run(session, p.id, c.id, sample=lambda: 0.02)  # draw under rate → flagged

    items = session.exec(select(ContentItem).order_by(ContentItem.id)).all()
    assert [i.spot_check for i in items] == [True, False, True]


def test_first_item_is_per_channel_not_per_product(session):
    """Each channel's inaugural item is flagged independently."""
    p = _product(session)
    _brief(session, p.id)
    c1 = _channel(session, p.id, ChannelType.REDDIT)
    c2 = _channel(session, p.id, ChannelType.BLOG)

    _run(session, p.id, c1.id, sample=lambda: 0.99)
    _run(session, p.id, c2.id, sample=lambda: 0.99)

    items = session.exec(select(ContentItem).order_by(ContentItem.id)).all()
    assert all(i.spot_check for i in items)


def test_flag_never_changes_status(session):
    """Flagging is orthogonal to the pipeline — a passing item still lands critic_passed."""
    p = _product(session)
    _brief(session, p.id)
    c = _channel(session, p.id)

    _run(session, p.id, c.id, sample=lambda: 0.0)  # forced flag

    item = session.exec(select(ContentItem)).one()
    assert item.spot_check is True
    assert item.status == ContentItemStatus.CRITIC_PASSED


# --- review-queue API (AC2) --------------------------------------------------------------------


@pytest.fixture
def client(engine):
    def _override():
        with Session(engine) as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c


def test_review_queue_returns_only_flagged_items_newest_first(client, engine):
    with Session(engine) as s:
        p = Product(name="acme", slug="acme")
        s.add(p)
        s.commit()
        s.refresh(p)
        flagged_old = ContentItem(
            product_id=p.id, channel_id=1, content_type="social", body="a", spot_check=True
        )
        unflagged = ContentItem(
            product_id=p.id, channel_id=1, content_type="social", body="b", spot_check=False
        )
        flagged_new = ContentItem(
            product_id=p.id, channel_id=1, content_type="social", body="c", spot_check=True
        )
        s.add_all([flagged_old, unflagged, flagged_new])
        s.commit()
        pid = p.id

    resp = client.get(f"/api/private/content/{pid}/spot-check")
    assert resp.status_code == 200
    bodies = [it["body"] for it in resp.json()]
    assert bodies == ["c", "a"]  # only flagged, newest (highest id) first
    assert all(it["spot_check"] for it in resp.json())


def test_review_queue_404s_for_unknown_product(client):
    assert client.get("/api/private/content/999/spot-check").status_code == 404
