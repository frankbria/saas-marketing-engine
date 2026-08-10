"""S4.1.1 (#82): operator-triggered crank from the dashboard.

The scheduled crank (S4.1) only fires on the hourly tick, gated on a per-product cadence that
defaults to weekly. That is right at steady state and useless while onboarding a product,
validating a single channel connection, or running the S6.4 acceptance sequence — hence a manual
trigger (PRD G5: the operator runs the cycle without touching code).

Two invariants get most of the attention here because they are the ones that would rot quietly:

1. **A manual crank must not skew the cadence.** `enqueue_due_cranks` decides due-ness by looking
   for a recent `crank` row, so a manual run recorded under the same `kind` would suppress the next
   scheduled crank for a whole cadence window. Manual runs are recorded as `crank_manual` and the
   cadence query never sees them.
2. **Manual cranks must not stack.** Two clicks (or a click while the scheduler's crank is in
   flight) must not fan out twice and spend tokens twice.

Real app, real SQLite, no mocks — same shape as test_crank.py / test_qa_gate.py.
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import get_session
from app.main import create_app
from app.models import (
    Channel,
    ChannelType,
    ConnectState,
    JobRun,
    JobStatus,
    LifecycleState,
    Product,
)
from app.modules.crank.crank import (
    MANUAL_CRANK_KIND,
    ContentType,
    enqueue_due_cranks,
    enqueue_manual_crank,
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


@pytest.fixture
def client(session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _product(session, *, state=LifecycleState.LIVE, slug="auto-author", cadence=None) -> Product:
    product = Product(
        name="Auto Author", slug=slug, lifecycle_state=state, crank_cadence_seconds=cadence
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def _channel(
    session,
    product_id,
    ctype=ChannelType.BLOG,
    *,
    enabled=True,
    autonomous=True,
    paused=False,
    connect_state=ConnectState.CONNECTED,
) -> Channel:
    channel = Channel(
        product_id=product_id,
        type=ctype,
        enabled=enabled,
        autonomous=autonomous,
        paused=paused,
        connect_state=connect_state,
    )
    session.add(channel)
    session.commit()
    session.refresh(channel)
    return channel


def _jobs(session, kind: str, product_id: int) -> list[JobRun]:
    return list(
        session.exec(
            select(JobRun).where(JobRun.kind == kind, JobRun.product_id == product_id)
        ).all()
    )


# --- enqueue ---------------------------------------------------------------------------------


def test_enqueues_a_manual_crank_for_a_live_product(client, session):
    product = _product(session)
    _channel(session, product.id)

    response = client.post(f"/api/private/crank/{product.id}")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == JobStatus.QUEUED
    queued = session.get(JobRun, body["job_id"])
    assert queued.kind == MANUAL_CRANK_KIND
    assert queued.product_id == product.id
    assert queued.channel_id is None  # whole-product crank


def test_unknown_product_is_404(client):
    assert client.post("/api/private/crank/999").status_code == 404


@pytest.mark.parametrize(
    "state", [state for state in LifecycleState if state != LifecycleState.LIVE]
)
def test_refuses_unless_the_product_is_live(client, session, state):
    product = _product(session, state=state, slug=f"p-{state}")

    response = client.post(f"/api/private/crank/{product.id}")

    assert response.status_code == 409
    assert "live" in response.json()["detail"]
    assert _jobs(session, MANUAL_CRANK_KIND, product.id) == []


# --- no stacking -----------------------------------------------------------------------------


@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.RUNNING])
def test_refuses_while_a_manual_crank_is_already_in_flight(client, session, status):
    product = _product(session)
    session.add(JobRun(kind=MANUAL_CRANK_KIND, product_id=product.id, status=status))
    session.commit()

    response = client.post(f"/api/private/crank/{product.id}")

    assert response.status_code == 409
    assert "already" in response.json()["detail"]
    assert len(_jobs(session, MANUAL_CRANK_KIND, product.id)) == 1


@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.RUNNING])
def test_refuses_while_the_scheduled_crank_is_in_flight(client, session, status):
    """A manual crank on top of the scheduler's own in-flight crank would double the fan-out."""
    product = _product(session)
    session.add(JobRun(kind="crank", product_id=product.id, status=status))
    session.commit()

    response = client.post(f"/api/private/crank/{product.id}")

    assert response.status_code == 409
    assert _jobs(session, MANUAL_CRANK_KIND, product.id) == []


@pytest.mark.parametrize("status", [JobStatus.DONE, JobStatus.FAILED])
def test_a_finished_crank_does_not_block_a_new_one(client, session, status):
    product = _product(session)
    _channel(session, product.id)
    session.add(JobRun(kind="crank", product_id=product.id, status=status))
    session.commit()

    assert client.post(f"/api/private/crank/{product.id}").status_code == 202


def test_an_in_flight_crank_on_another_product_does_not_block(client, session):
    other = _product(session, slug="other")
    product = _product(session, slug="mine")
    _channel(session, product.id)
    session.add(JobRun(kind=MANUAL_CRANK_KIND, product_id=other.id, status=JobStatus.QUEUED))
    session.commit()

    assert client.post(f"/api/private/crank/{product.id}").status_code == 202


def test_concurrent_enqueues_produce_exactly_one_crank(session):
    """The check and the insert must be atomic against each other.

    The dashboard POST and the scheduler's `_crank_tick` run in the same process on different
    threads, so a plain read-then-write lets both observe an empty queue and both enqueue —
    a doubled fan-out that spends tokens twice and publishes twice. Hammering the guarded
    entry point from several threads at once fails loudly without the lock.
    """
    product = _product(session)
    _channel(session, product.id)
    results: list[JobRun | None] = []
    barrier = threading.Barrier(8)

    def attempt() -> None:
        barrier.wait()  # release all threads into the critical section together
        results.append(enqueue_manual_crank(session, product.id))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(job is not None for job in results) == 1
    assert len(_jobs(session, MANUAL_CRANK_KIND, product.id)) == 1


def test_losing_the_enqueue_race_is_reported_as_409(client, session, monkeypatch):
    """The route's own pre-check can pass and the guarded enqueue still refuse.

    That happens when the scheduler's tick commits between the two. Driving it with real threads
    would be a timing test; patching the seam pins the branch deterministically.
    """
    product = _product(session)
    _channel(session, product.id)
    monkeypatch.setattr("app.api.private.crank.enqueue_manual_crank", lambda *args, **kwargs: None)

    response = client.post(f"/api/private/crank/{product.id}")

    assert response.status_code == 409
    assert "concurrently" in response.json()["detail"]


# --- cadence is not skewed -------------------------------------------------------------------


def test_a_manual_crank_does_not_suppress_the_next_scheduled_crank(client, session):
    """The whole point of a separate kind: `enqueue_due_cranks` must not see manual runs.

    A product that has never been cranked is due. Cranking it manually must leave it due — the
    operator's demo run should not cost the product its next scheduled cycle.
    """
    product = _product(session)
    _channel(session, product.id)
    now = datetime.now(UTC)

    assert client.post(f"/api/private/crank/{product.id}").status_code == 202

    due = enqueue_due_cranks(session, now)

    assert [job.product_id for job in due] == [product.id]
    assert due[0].kind == "crank"


def test_a_scheduled_crank_still_suppresses_the_next_scheduled_crank(session):
    """Guard against 'fixing' the above by making the cadence query blind to everything."""
    _product(session)
    now = datetime.now(UTC)

    assert len(enqueue_due_cranks(session, now)) == 1
    assert enqueue_due_cranks(session, now + timedelta(seconds=60)) == []


# --- fan-out ---------------------------------------------------------------------------------


def test_a_manual_crank_fans_out_every_eligible_channel(client, session):
    product = _product(session)
    blog = _channel(session, product.id, ChannelType.BLOG)
    reddit = _channel(session, product.id, ChannelType.REDDIT)

    client.post(f"/api/private/crank/{product.id}")
    run_due_jobs(session)

    children = _jobs(session, "generate", product.id)
    assert {child.channel_id for child in children} == {blog.id, reddit.id}
    assert {child.content_type for child in children} == {
        ContentType.BLOG.value,
        ContentType.SOCIAL.value,
    }


def test_channel_id_narrows_the_fan_out_to_one_channel(client, session):
    """Validating a single channel connection without spending tokens on all of them."""
    product = _product(session)
    blog = _channel(session, product.id, ChannelType.BLOG)
    _channel(session, product.id, ChannelType.REDDIT)

    response = client.post(f"/api/private/crank/{product.id}", params={"channel_id": blog.id})

    assert response.status_code == 202
    assert session.get(JobRun, response.json()["job_id"]).channel_id == blog.id

    run_due_jobs(session)

    children = _jobs(session, "generate", product.id)
    assert [child.channel_id for child in children] == [blog.id]
    assert [child.content_type for child in children] == [ContentType.BLOG.value]


def test_channel_belonging_to_another_product_is_404(client, session):
    product = _product(session)
    other = _product(session, slug="other")
    foreign = _channel(session, other.id)

    response = client.post(f"/api/private/crank/{product.id}", params={"channel_id": foreign.id})

    assert response.status_code == 404
    assert _jobs(session, MANUAL_CRANK_KIND, product.id) == []


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"enabled": False}, "disabled"),
        ({"paused": True}, "paused"),
        ({"autonomous": False}, "not autonomous"),
        ({"connect_state": ConnectState.FAILED}, "connection failed"),
    ],
)
def test_an_ineligible_channel_is_refused_with_the_reason(client, session, kwargs, reason):
    """Refuse up front rather than enqueue a crank that silently fans out to nothing."""
    product = _product(session)
    channel = _channel(session, product.id, **kwargs)

    response = client.post(f"/api/private/crank/{product.id}", params={"channel_id": channel.id})

    assert response.status_code == 409
    assert reason in response.json()["detail"]
    assert _jobs(session, MANUAL_CRANK_KIND, product.id) == []


def test_a_manual_crank_with_no_eligible_channels_is_refused(client, session):
    """Nothing to generate — say so instead of queueing a job that does nothing."""
    product = _product(session)
    _channel(session, product.id, paused=True)

    response = client.post(f"/api/private/crank/{product.id}")

    assert response.status_code == 409
    assert "no eligible channels" in response.json()["detail"]
    assert _jobs(session, MANUAL_CRANK_KIND, product.id) == []
