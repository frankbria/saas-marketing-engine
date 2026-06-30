"""S3.1: generate a click-through QA checklist → qa_checklist_item rows.

Deterministic unit tests drive the worker wiring, persistence, coverage contract, budget gate,
the qa-state gate, and idempotent regeneration with no network. The integration test makes a real
Anthropic call and is skipped unless SME_ANTHROPIC_API_KEY is set (honors the no-mock rule without
spending money in CI).
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app.ai.client import QaChecklist, QaStep
from app.db import get_session
from app.main import create_app
from app.models import (
    JobStatus,
    LifecycleState,
    Product,
    QaChecklistItem,
    QaItemStatus,
    StrategyBrief,
)
from app.modules.qa import checklist as checklist_mod
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


def _make_product_with_brief(session, *, budget=0, state=LifecycleState.QA):
    product = Product(
        name="Auto Author",
        slug="auto-author",
        repo_local_path="/tmp/x",
        description="AI book-writing tool",
        marketing_domain="autoauthor.example",
        price_amount_cents=2900,
        price_interval="month",
        token_budget_cents_month=budget,
        lifecycle_state=state,
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    brief = StrategyBrief(
        product_id=product.id,
        icp_json='{"segment": "indie authors", "description": "self-pub", "firmographics": []}',
        pain_points_json="[]",
        positioning="The fastest way to a finished manuscript.",
        channel_plan_json="[]",
        content_pillars_json='["craft"]',
        cadence_json="{}",
    )
    session.add(brief)
    session.commit()
    return product


def _checklist(steps=None) -> QaChecklist:
    steps = steps or [
        QaStep(
            instruction="Open autoauthor.example and verify the headline renders.",
            area="product",
            blocking=False,
        ),
        QaStep(
            instruction="Click Sign up, submit an email, verify the welcome screen.",
            area="product",
            blocking=True,
        ),
        QaStep(
            instruction="Click Subscribe and verify Stripe checkout shows $29/month.",
            area="funnel",
            blocking=True,
        ),
    ]
    return QaChecklist(steps=steps)


def _stub(checklist: QaChecklist, cost: int = 7):
    def gen(_product, _brief, _remaining):
        return checklist, cost

    return gen


# --- worker wiring + persistence ---------------------------------------------------------------


def test_generate_persists_ordered_rows(session):
    product = _make_product_with_brief(session)
    job = enqueue(session, "qa_checklist", product_id=product.id)

    cost = checklist_mod.generate_qa_checklist_items(job, session, generate=_stub(_checklist()))
    session.commit()

    assert cost == 7
    rows = session.exec(
        select(QaChecklistItem)
        .where(QaChecklistItem.product_id == product.id)
        .order_by(QaChecklistItem.ord)
    ).all()
    assert [r.ord for r in rows] == [1, 2, 3]
    assert all(r.status == QaItemStatus.PENDING for r in rows)
    assert [r.blocking for r in rows] == [False, True, True]
    assert "Sign up" in rows[1].instruction


def test_full_enqueue_run_path_records_cost_and_done(session, monkeypatch):
    product = _make_product_with_brief(session)
    monkeypatch.setattr(checklist_mod, "_GENERATE", _stub(_checklist(), cost=11))
    job = enqueue(session, "qa_checklist", product_id=product.id)

    run_due_jobs(session)

    session.refresh(job)
    assert job.status == JobStatus.DONE
    assert job.token_cost_cents == 11
    assert len(session.exec(select(QaChecklistItem)).all()) == 3
    # generation does NOT transition lifecycle (S3.2 owns qa → live)
    session.refresh(product)
    assert product.lifecycle_state == LifecycleState.QA


# --- coverage contract -------------------------------------------------------------------------


def test_missing_funnel_coverage_raises(session):
    product = _make_product_with_brief(session)
    only_product = _checklist(
        [
            QaStep(
                instruction="Open the app and log in, verify dashboard.",
                area="product",
                blocking=True,
            )
        ]
    )
    job = enqueue(session, "qa_checklist", product_id=product.id)
    with pytest.raises(RuntimeError, match="missing coverage: funnel"):
        checklist_mod.generate_qa_checklist_items(job, session, generate=_stub(only_product))


# --- gates -------------------------------------------------------------------------------------


def test_handler_refuses_when_not_in_qa(session):
    product = _make_product_with_brief(session, state=LifecycleState.SETUP_DONE)
    job = enqueue(session, "qa_checklist", product_id=product.id)
    with pytest.raises(RuntimeError, match="not qa"):
        checklist_mod.generate_qa_checklist_items(job, session, generate=_stub(_checklist()))


def test_handler_refuses_without_brief(session):
    product = Product(name="No Brief", slug="no-brief", lifecycle_state=LifecycleState.QA)
    session.add(product)
    session.commit()
    session.refresh(product)
    job = enqueue(session, "qa_checklist", product_id=product.id)
    with pytest.raises(RuntimeError, match="no strategy brief"):
        checklist_mod.generate_qa_checklist_items(job, session, generate=_stub(_checklist()))


def test_over_budget_refuses_before_generating(session):
    product = _make_product_with_brief(session, budget=1)

    # generator that explodes if called — the budget gate must short-circuit before it
    def boom(*_a):  # pragma: no cover - must not run
        raise AssertionError("generate called despite over-budget")

    job = enqueue(session, "qa_checklist", product_id=product.id)
    # spend the budget via a prior costed job_run so month-to-date >= budget
    spent = enqueue(session, "qa_checklist", product_id=product.id)
    spent.status = JobStatus.DONE
    spent.token_cost_cents = 5
    session.add(spent)
    session.commit()
    with pytest.raises(RuntimeError, match="over monthly token budget"):
        checklist_mod.generate_qa_checklist_items(job, session, generate=boom)


# --- idempotent regeneration -------------------------------------------------------------------


def test_regeneration_replaces_rows(session):
    product = _make_product_with_brief(session)
    job1 = enqueue(session, "qa_checklist", product_id=product.id)
    checklist_mod.generate_qa_checklist_items(job1, session, generate=_stub(_checklist()))
    session.commit()

    smaller = _checklist(
        [
            QaStep(instruction="Open site, verify it loads.", area="product", blocking=True),
            QaStep(instruction="Checkout, verify $29/mo.", area="funnel", blocking=True),
        ]
    )
    job2 = enqueue(session, "qa_checklist", product_id=product.id)
    checklist_mod.generate_qa_checklist_items(job2, session, generate=_stub(smaller))
    session.commit()

    rows = session.exec(
        select(QaChecklistItem)
        .where(QaChecklistItem.product_id == product.id)
        .order_by(QaChecklistItem.ord)
    ).all()
    assert [r.ord for r in rows] == [1, 2]  # fully replaced, no stale ord=3


# --- API routes --------------------------------------------------------------------------------


@pytest.fixture
def client(session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_post_checklist_enqueues_when_qa(client, session):
    product = _make_product_with_brief(session)
    resp = client.post(f"/api/private/qa/{product.id}/checklist")
    assert resp.status_code == 202
    assert resp.json()["status"] == JobStatus.QUEUED


def test_post_checklist_409_when_not_qa(client, session):
    product = _make_product_with_brief(session, state=LifecycleState.SETUP_DONE)
    resp = client.post(f"/api/private/qa/{product.id}/checklist")
    assert resp.status_code == 409


def test_get_checklist_lists_rows(client, session):
    product = _make_product_with_brief(session)
    job = enqueue(session, "qa_checklist", product_id=product.id)
    checklist_mod.generate_qa_checklist_items(job, session, generate=_stub(_checklist()))
    session.commit()

    resp = client.get(f"/api/private/qa/{product.id}/checklist")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 3
    assert [r["ord"] for r in body] == [1, 2, 3]


# --- real API integration (skipped without a key) ----------------------------------------------


@pytest.mark.skipif(
    not os.getenv("SME_ANTHROPIC_API_KEY"), reason="needs SME_ANTHROPIC_API_KEY for a real call"
)
def test_real_generation_covers_both_areas(session):
    product = _make_product_with_brief(session)
    job = enqueue(session, "qa_checklist", product_id=product.id)
    cost = checklist_mod.generate_qa_checklist_items(job, session)  # real _real_generate
    session.commit()
    assert cost > 0
    rows = session.exec(
        select(QaChecklistItem).where(QaChecklistItem.product_id == product.id)
    ).all()
    assert len(rows) >= 2
