"""S4.1: scheduled crank — per-product cadence trigger + autonomous-channel × content-type fan-out.

Drives the pure functions (`enqueue_due_cranks`) and the registered handlers directly against a
real SQLite file — deterministic, no scheduler thread, `now` injected so cadence is controllable.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import (
    Channel,
    ChannelType,
    JobRun,
    JobStatus,
    LifecycleState,
    Product,
)
from app.modules.crank.crank import (
    WEEKLY_SECONDS,
    ContentType,
    enqueue_due_cranks,
)
from app.worker import run_due_jobs


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


def _product(session, *, slug, state=LifecycleState.LIVE, cadence=None):
    p = Product(name=slug, slug=slug, lifecycle_state=state, crank_cadence_seconds=cadence)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _channel(session, product_id, ctype, *, enabled=True, autonomous=True, paused=False):
    c = Channel(
        product_id=product_id, type=ctype, enabled=enabled, autonomous=autonomous, paused=paused
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)


# --- enqueue_due_cranks (the per-product cadence trigger) -------------------------------------


def test_only_live_products_crank(session):
    live = _product(session, slug="live", state=LifecycleState.LIVE)
    _product(session, slug="draft", state=LifecycleState.DRAFT)
    _product(session, slug="qa", state=LifecycleState.QA)
    _product(session, slug="paused", state=LifecycleState.PAUSED)

    enqueued = enqueue_due_cranks(session, NOW)

    assert [(j.kind, j.product_id) for j in enqueued] == [("crank", live.id)]


def test_never_cranked_product_is_due(session):
    p = _product(session, slug="live")
    enqueued = enqueue_due_cranks(session, NOW)
    assert len(enqueued) == 1
    assert enqueued[0].product_id == p.id


def _seed_crank(session, product_id, created_at):
    """Insert a past crank row with a controlled timestamp (enqueue() stamps real wall-clock)."""
    session.add(JobRun(kind="crank", product_id=product_id, created_at=created_at))
    session.commit()


def test_not_due_within_cadence(session):
    p = _product(session, slug="live")  # default weekly
    _seed_crank(session, p.id, NOW - timedelta(days=1))  # cranked yesterday — inside the window
    again = enqueue_due_cranks(session, NOW)
    assert again == []
    assert len(session.exec(select(JobRun).where(JobRun.kind == "crank")).all()) == 1


def test_due_after_cadence_elapses(session):
    p = _product(session, slug="live")  # default weekly
    _seed_crank(session, p.id, NOW - timedelta(seconds=WEEKLY_SECONDS + 1))  # just over a week ago
    again = enqueue_due_cranks(session, NOW)
    assert [j.product_id for j in again] == [p.id]
    assert len(session.exec(select(JobRun).where(JobRun.kind == "crank")).all()) == 2


@pytest.mark.parametrize("bad", [0, -1, -3600])
def test_nonpositive_cadence_falls_back_to_weekly(session, bad):
    # A non-positive cadence must NOT push the due-cutoff into the future (which would re-enqueue
    # a crank on every poll); it's clamped to the weekly default. Cranked yesterday → still not due.
    p = _product(session, slug="bad", cadence=bad)
    _seed_crank(session, p.id, NOW - timedelta(days=1))
    assert enqueue_due_cranks(session, NOW) == []


def test_custom_cadence_per_product(session):
    fast = _product(session, slug="fast", cadence=3600)  # hourly
    slow = _product(session, slug="slow", cadence=None)  # weekly default
    two_h_ago = NOW - timedelta(hours=2)
    _seed_crank(session, fast.id, two_h_ago)
    _seed_crank(session, slow.id, two_h_ago)

    enqueued = enqueue_due_cranks(session, NOW)
    # Hourly product is due again (2h > 1h); the weekly one is not.
    assert [j.product_id for j in enqueued] == [fast.id]


# --- crank handler fan-out ---------------------------------------------------------------------


def test_crank_fans_out_per_autonomous_channel_and_content_type(session):
    p = _product(session, slug="live")
    blog = _channel(session, p.id, ChannelType.BLOG)
    reddit = _channel(session, p.id, ChannelType.REDDIT)

    enqueue_due_cranks(session, NOW)
    run_due_jobs(session)  # runs the crank job → enqueues generate children

    crank = session.exec(select(JobRun).where(JobRun.kind == "crank")).one()
    assert crank.status == JobStatus.DONE

    children = session.exec(select(JobRun).where(JobRun.kind == "generate")).all()
    cells = {(c.channel_id, c.content_type) for c in children}
    assert cells == {
        (blog.id, ContentType.BLOG.value),
        (reddit.id, ContentType.SOCIAL.value),
    }
    assert all(c.product_id == p.id for c in children)
    assert all(c.status == JobStatus.QUEUED for c in children)


def test_crank_skips_paused_disabled_and_nonautonomous_channels(session):
    p = _product(session, slug="live")
    _channel(session, p.id, ChannelType.BLOG, paused=True)
    _channel(session, p.id, ChannelType.REDDIT, enabled=False)
    _channel(session, p.id, ChannelType.X, autonomous=False)  # X is human-assisted anyway

    enqueue_due_cranks(session, NOW)
    run_due_jobs(session)

    children = session.exec(select(JobRun).where(JobRun.kind == "generate")).all()
    assert children == []


def test_crank_with_no_channels_does_nothing_but_succeeds(session):
    _product(session, slug="live")
    enqueue_due_cranks(session, NOW)
    run_due_jobs(session)
    crank = session.exec(select(JobRun).where(JobRun.kind == "crank")).one()
    assert crank.status == JobStatus.DONE
    assert session.exec(select(JobRun).where(JobRun.kind == "generate")).all() == []


# --- generate seam (S4.2 fills the real pipeline) ----------------------------------------------


def test_generate_seam_validates_cell_and_succeeds(session):
    p = _product(session, slug="live")
    c = _channel(session, p.id, ChannelType.BLOG)
    job = JobRun(
        kind="generate", product_id=p.id, channel_id=c.id, content_type=ContentType.BLOG.value
    )
    session.add(job)
    session.commit()

    run_due_jobs(session)
    session.refresh(job)
    assert job.status == JobStatus.DONE
    assert job.token_cost_cents == 0


def test_generate_without_cell_identity_fails(session):
    job = JobRun(kind="generate", product_id=None)  # missing channel_id/content_type
    session.add(job)
    session.commit()

    run_due_jobs(session)
    session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.attempts == 1  # config error — no point retrying


# --- end-to-end round trip ---------------------------------------------------------------------


def test_crank_to_generate_round_trip(session):
    p = _product(session, slug="live")
    _channel(session, p.id, ChannelType.BLOG)

    enqueue_due_cranks(session, NOW)
    run_due_jobs(session)  # crank → DONE, enqueues 1 generate child (QUEUED)
    run_due_jobs(session)  # generate child → DONE

    jobs = {j.kind: j.status for j in session.exec(select(JobRun)).all()}
    assert jobs == {"crank": JobStatus.DONE, "generate": JobStatus.DONE}
