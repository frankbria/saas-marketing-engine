"""S1.3: Marketing Brief → Pricing recommendation (product.price_*).

Deterministic unit tests drive the worker wiring, persistence, budget gate, and the cc_sub-only
constraint with no network. The integration test makes a real Anthropic call and is skipped unless
SME_ANTHROPIC_API_KEY is set (honors the no-mock rule without spending money in CI).
"""

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app import worker
from app.ai.client import PricingRecommendation
from app.config import settings
from app.models import JobStatus, LifecycleState, MonetizationModel, Product, StrategyBrief
from app.modules.strategy import pricing as pricing_mod
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


def _make_product_with_brief(session, *, budget=0, model=MonetizationModel.CC_SUB):
    product = Product(
        name="Auto Author",
        slug="auto-author",
        repo_local_path="/tmp/x",
        description="AI book-writing tool",
        monetization_model=model,
        token_budget_cents_month=budget,
        lifecycle_state=LifecycleState.STRATEGY,  # S1.1 already ran
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
        content_pillars_json='["craft", "publishing", "marketing"]',
        cadence_json="{}",
    )
    session.add(brief)
    session.commit()
    return product


def _stub_pricing():
    return PricingRecommendation(price_amount_cents=2900, price_interval="month")


# ---- schema --------------------------------------------------------------------------------


def test_pricing_schema_rejects_nonpositive_amount():
    with pytest.raises(ValueError):
        PricingRecommendation(price_amount_cents=0, price_interval="month")


def test_pricing_schema_rejects_unknown_interval():
    with pytest.raises(ValueError):
        PricingRecommendation(price_amount_cents=2900, price_interval="weekly")


# ---- persistence + state -------------------------------------------------------------------


def test_generate_persists_pricing_and_keeps_state(session):
    product = _make_product_with_brief(session)
    job = enqueue(session, "pricing", product_id=product.id)

    cost = pricing_mod.generate_product_pricing(
        job, session, generate=lambda p, b, r: (_stub_pricing(), 9)
    )

    assert cost == 9
    session.commit()  # the worker commits after the handler; mimic that before asserting
    session.refresh(product)
    assert product.price_amount_cents == 2900
    assert product.price_interval == "month"
    assert product.lifecycle_state == LifecycleState.STRATEGY  # pricing doesn't change state


def test_no_brief_raises(session):
    product = Product(name="No Brief", slug="no-brief", token_budget_cents_month=0)
    session.add(product)
    session.commit()
    session.refresh(product)
    job = enqueue(session, "pricing", product_id=product.id)

    with pytest.raises(RuntimeError, match="no strategy brief"):
        pricing_mod.generate_product_pricing(
            job, session, generate=lambda p, b, r: (_stub_pricing(), 1)
        )


def test_non_cc_sub_raises(session):
    # trial/freemium remain unwired in v1 — pricing only applies to cc_sub.
    product = _make_product_with_brief(session, model=MonetizationModel.TRIAL)
    job = enqueue(session, "pricing", product_id=product.id)

    def _boom(_p, _b, _r):  # must not be reached
        raise AssertionError("generate called for a non-cc_sub product")

    with pytest.raises(RuntimeError, match="cc_sub"):
        pricing_mod.generate_product_pricing(job, session, generate=_boom)


# ---- budget gate ---------------------------------------------------------------------------


def test_budget_exceeded_raises_before_generate(session):
    product = _make_product_with_brief(session, budget=100)
    spent = enqueue(session, "pricing", product_id=product.id)
    spent.token_cost_cents = 100
    session.add(spent)
    session.commit()

    job = enqueue(session, "pricing", product_id=product.id)

    def _boom(_p, _b, _r):  # must not be reached
        raise AssertionError("generate called despite over-budget")

    with pytest.raises(RuntimeError, match="over monthly token budget"):
        pricing_mod.generate_product_pricing(job, session, generate=_boom)


def test_zero_budget_is_unlimited(session):
    product = _make_product_with_brief(session, budget=0)
    job = enqueue(session, "pricing", product_id=product.id)
    captured = {}

    def _capture(p, b, r):
        captured["remaining"] = r
        return _stub_pricing(), 1

    pricing_mod.generate_product_pricing(job, session, generate=_capture)
    assert captured["remaining"] is None


def test_remaining_budget_is_capped_to_unspent(session):
    product = _make_product_with_brief(session, budget=100)
    spent = enqueue(session, "pricing", product_id=product.id)
    spent.token_cost_cents = 40
    session.add(spent)
    session.commit()
    job = enqueue(session, "pricing", product_id=product.id)

    captured = {}

    def _capture(p, b, r):
        captured["remaining"] = r
        return _stub_pricing(), 1

    pricing_mod.generate_product_pricing(job, session, generate=_capture)
    assert captured["remaining"] == 60  # 100 cap − 40 already spent this month


def test_real_generate_reserves_budget_for_synthesis(session, monkeypatch):
    product = _make_product_with_brief(session, budget=0)
    brief = session.exec(select(StrategyBrief).where(StrategyBrief.product_id == product.id)).one()

    monkeypatch.setattr(pricing_mod, "build_client", lambda: object())

    def _no_call(*a, **k):
        raise AssertionError("recommend_pricing must not run when it can't be afforded")

    monkeypatch.setattr(pricing_mod, "recommend_pricing", _no_call)

    # remaining 2; the reserved Opus cost (≥3¢ for 1000 output tokens) pushes over 2
    with pytest.raises(RuntimeError, match="reserve for pricing"):
        pricing_mod._real_generate(product, brief, 2)


# ---- worker path ---------------------------------------------------------------------------


def test_worker_runs_handler_and_records_cost(session, monkeypatch):
    product = _make_product_with_brief(session)
    monkeypatch.setattr(pricing_mod, "_GENERATE", lambda p, b, r: (_stub_pricing(), 55))
    job = enqueue(session, "pricing", product_id=product.id)

    assert "pricing" in worker._HANDLERS  # registered at import
    run_due_jobs(session)

    session.refresh(job)
    assert job.status == JobStatus.DONE
    assert job.token_cost_cents == 55  # cost recorded to job_run


# ---- API route -----------------------------------------------------------------------------


def test_route_enqueues_pricing_job(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app

    product = _make_product_with_brief(session)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        resp = client.post(f"/api/private/strategy/{product.id}/pricing")
    assert resp.status_code == 202
    assert resp.json()["status"] == JobStatus.QUEUED


def test_route_404_for_missing_product(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        resp = client.post("/api/private/strategy/999/pricing")
    assert resp.status_code == 404


def test_route_400_when_no_brief(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app

    product = Product(name="No Brief", slug="no-brief", token_budget_cents_month=0)
    session.add(product)
    session.commit()
    session.refresh(product)

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        resp = client.post(f"/api/private/strategy/{product.id}/pricing")
    assert resp.status_code == 400


def test_route_400_when_not_cc_sub(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app

    product = _make_product_with_brief(session, model=MonetizationModel.FREEMIUM)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        resp = client.post(f"/api/private/strategy/{product.id}/pricing")
    assert resp.status_code == 400


# ---- owner-editable (PATCH) ----------------------------------------------------------------


def test_owner_can_edit_price_fields(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app

    product = _make_product_with_brief(session)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        resp = client.patch(
            f"/api/private/products/{product.id}",
            json={"price_amount_cents": 4900, "price_interval": "year"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["price_amount_cents"] == 4900
    assert body["price_interval"] == "year"


def test_owner_edit_rejects_nonpositive_price(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app

    product = _make_product_with_brief(session)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        resp = client.patch(f"/api/private/products/{product.id}", json={"price_amount_cents": 0})
    assert resp.status_code == 422


# ---- real-API integration (key-gated) ------------------------------------------------------


@pytest.mark.skipif(
    settings.anthropic_api_key is None,
    reason="requires SME_ANTHROPIC_API_KEY (real API call); set it in the env or backend/.env",
)
def test_integration_real_pricing(session):
    product = _make_product_with_brief(session)
    job = enqueue(session, "pricing", product_id=product.id)

    cost = pricing_mod.generate_product_pricing(job, session, generate=pricing_mod._real_generate)

    assert cost > 0  # real token spend recorded
    session.commit()
    session.refresh(product)
    assert product.price_amount_cents > 0  # a populated price
    assert product.price_interval in ("month", "year")
