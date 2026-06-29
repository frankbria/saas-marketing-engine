"""S2.6: setup_channels handler — profiles folded onto channels + deterministic human checklist.

Deterministic unit tests drive the worker wiring, persistence, idempotency, budget gate, and the
brand/brief preconditions with no network (the LLM call is injected). The integration test that
makes a real Opus call is the operator's (key-gated elsewhere); these assert the wiring.
"""

import json

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.ai.client import ChannelProfile, ChannelProfiles
from app.models import (
    Channel,
    ChannelType,
    ConnectState,
    LifecycleState,
    Product,
    SetupChecklistItem,
    SetupItemStatus,
    StrategyBrief,
)
from app.modules.setup import channels as channels_mod
from app.modules.setup.channels import (
    channel_types_from_brief,
    map_channel_type,
    setup_product_channels,
)
from app.worker import enqueue, run_due_jobs

_BRAND_JSON = json.dumps(
    {
        "name": "Auto Author",
        "tone": "confident, helpful",
        "voice_descriptors": [{"descriptor": "clear", "guidance": "short sentences"}],
        "visual_seeds": ["ink", "paper"],
    }
)

_CHANNEL_PLAN = json.dumps(
    [
        {"channel": "Reddit", "rationale": "communities", "priority": 1},
        {"channel": "Blog / SEO", "rationale": "organic", "priority": 2},
        {"channel": "X (Twitter)", "rationale": "buzz", "priority": 3},
    ]
)


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


def _make_product(session, *, brand=_BRAND_JSON, budget=0, plan=_CHANNEL_PLAN):
    product = Product(
        name="Auto Author",
        slug="auto-author",
        description="AI book writer",
        brand_json=brand,
        marketing_domain="autoauthor.app",
        token_budget_cents_month=budget,
        lifecycle_state=LifecycleState.SETUP_READY,
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    if plan is not None:
        session.add(
            StrategyBrief(
                product_id=product.id,
                icp_json="{}",
                pain_points_json="[]",
                positioning="best",
                channel_plan_json=plan,
                content_pillars_json="[]",
                cadence_json="{}",
            )
        )
        session.commit()
    return product


def _stub_generate(cost=7):
    def gen(product, brand_kit, channel_types, remaining):
        profiles = [
            ChannelProfile(type=t, handle=f"{t}_auto", bio=f"{t} bio", profile_copy=f"{t} about")
            for t in channel_types
        ]
        return ChannelProfiles(profiles=profiles), cost

    return gen


# ---- mapping ----------------------------------------------------------------------------


def test_map_channel_type_known_names():
    assert map_channel_type("Reddit") == ChannelType.REDDIT
    assert map_channel_type("Blog / SEO") == ChannelType.BLOG
    assert map_channel_type("X (Twitter)") == ChannelType.X
    assert map_channel_type("YouTube") == ChannelType.YOUTUBE
    assert map_channel_type("Instagram") == ChannelType.INSTAGRAM
    assert map_channel_type("carrier pigeon") is None


def test_channel_types_from_brief_ordered_and_deduped(session):
    from sqlmodel import select

    product = _make_product(session)
    brief = session.exec(select(StrategyBrief).where(StrategyBrief.product_id == product.id)).one()
    assert channel_types_from_brief(brief) == [
        ChannelType.REDDIT,
        ChannelType.BLOG,
        ChannelType.X,
    ]


# ---- happy path -------------------------------------------------------------------------


def test_setup_creates_channels_with_profiles(session):
    from sqlmodel import select

    product = _make_product(session)
    job = enqueue(session, "setup_channels", product_id=product.id)
    cost = setup_product_channels(job, session, generate=_stub_generate(cost=11))
    session.commit()

    assert cost == 11
    chans = session.exec(select(Channel)).all()
    by_type = {c.type: c for c in chans}
    assert set(by_type) == {ChannelType.REDDIT, ChannelType.BLOG, ChannelType.X}

    # autonomous: blog/reddit yes, x no
    assert by_type[ChannelType.REDDIT].autonomous is True
    assert by_type[ChannelType.BLOG].autonomous is True
    assert by_type[ChannelType.X].autonomous is False

    # profile folded incl. deterministic warm-up note
    prof = json.loads(by_type[ChannelType.REDDIT].profile_json)
    assert prof["handle"] == "reddit_auto"
    assert "before sharing any product links" in prof["warmup_note"]
    # connect_state starts pending until a token is posted
    assert by_type[ChannelType.X].connect_state == ConnectState.PENDING


def test_checklist_emitted_ordered_with_required_steps(session):
    product = _make_product(session)
    job = enqueue(session, "setup_channels", product_id=product.id)
    setup_product_channels(job, session, generate=_stub_generate())
    session.commit()

    from sqlmodel import select

    items = session.exec(
        select(SetupChecklistItem).where(SetupChecklistItem.product_id == product.id)
    ).all()
    # 3 channels × {account, tos, oauth} + 3 product-wide {dns, email_auth, payments}
    assert len(items) == 3 * 3 + 3
    cats = {i.category for i in items}
    assert {"account", "tos", "oauth", "dns", "email_auth", "payments"} <= cats

    # SPF/DKIM/DMARC explicitly present (AC) on the product-wide email_auth item
    email_item = next(i for i in items if i.category == "email_auth")
    assert "SPF" in email_item.instruction and "DKIM" in email_item.instruction
    assert "DMARC" in email_item.instruction

    # ords are unique and contiguous from 0
    ords = sorted(i.ord for i in items)
    assert ords == list(range(len(items)))


def test_setup_is_idempotent_and_preserves_progress(session):
    product = _make_product(session)
    job = enqueue(session, "setup_channels", product_id=product.id)
    setup_product_channels(job, session, generate=_stub_generate())
    session.commit()

    from sqlmodel import select

    # operator marks one item done + a channel connected
    item = session.exec(select(SetupChecklistItem)).first()
    item.status = SetupItemStatus.DONE
    chan = session.exec(select(Channel)).first()
    chan.connect_state = ConnectState.CONNECTED
    session.add(item)
    session.add(chan)
    session.commit()
    done_id, conn_type = item.id, chan.type

    # re-run setup
    job2 = enqueue(session, "setup_channels", product_id=product.id)
    setup_product_channels(job2, session, generate=_stub_generate())
    session.commit()

    chans = session.exec(select(Channel)).all()
    items = session.exec(select(SetupChecklistItem)).all()
    assert len(chans) == 3  # no duplicate channels
    assert len(items) == 12  # no duplicate checklist items
    # progress preserved
    assert session.get(SetupChecklistItem, done_id).status == SetupItemStatus.DONE
    assert next(c for c in chans if c.type == conn_type).connect_state == ConnectState.CONNECTED


# ---- worker path ------------------------------------------------------------------------


def test_runs_through_worker_loop(session, monkeypatch):
    from sqlmodel import select

    from app.models import JobRun, JobStatus

    product = _make_product(session)
    monkeypatch.setattr(channels_mod, "_GENERATE", _stub_generate(cost=5))
    enqueue(session, "setup_channels", product_id=product.id)
    run_due_jobs(session)

    job = session.exec(select(JobRun)).first()
    assert job.status == JobStatus.DONE
    assert job.token_cost_cents == 5
    assert len(session.exec(select(Channel)).all()) == 3


# ---- preconditions / gates --------------------------------------------------------------


def test_requires_brand_kit(session):
    product = _make_product(session, brand=None)
    job = enqueue(session, "setup_channels", product_id=product.id)
    with pytest.raises(RuntimeError, match="brand kit"):
        setup_product_channels(job, session, generate=_stub_generate())


def test_requires_recognized_channels(session):
    plan = json.dumps([{"channel": "carrier pigeon", "priority": 1}])
    product = _make_product(session, plan=plan)
    job = enqueue(session, "setup_channels", product_id=product.id)
    with pytest.raises(RuntimeError, match="no recognized channels"):
        setup_product_channels(job, session, generate=_stub_generate())


def test_incomplete_profiles_response_fails(session):
    """A model response missing a requested channel must fail, not persist blank profiles."""
    product = _make_product(session)

    def gen_missing_one(product, brand_kit, channel_types, remaining):
        # drop the last requested channel
        kept = channel_types[:-1]
        profiles = [
            ChannelProfile(type=t, handle=f"{t}_h", bio="b", profile_copy="c") for t in kept
        ]
        return ChannelProfiles(profiles=profiles), 3

    job = enqueue(session, "setup_channels", product_id=product.id)
    with pytest.raises(RuntimeError, match="omitted profiles"):
        setup_product_channels(job, session, generate=gen_missing_one)


def test_budget_exceeded_blocks(session):
    product = _make_product(session, budget=1)
    # bury the product over budget with a prior job cost
    from app.models import JobRun, JobStatus

    session.add(JobRun(product_id=product.id, kind="x", status=JobStatus.DONE, token_cost_cents=10))
    session.commit()
    job = enqueue(session, "setup_channels", product_id=product.id)
    with pytest.raises(RuntimeError, match="budget"):
        setup_product_channels(job, session, generate=_stub_generate())
