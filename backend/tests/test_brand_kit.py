"""S1.2: Marketing Brief → Brand Kit (product.brand_json).

Deterministic unit tests drive the worker wiring, persistence, and budget gate with no network.
The integration test makes a real Anthropic call and is skipped unless SME_ANTHROPIC_API_KEY is
set (honors the no-mock rule without spending money in CI).
"""

import json

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app import worker
from app.ai.client import BrandKit, VoiceDescriptor
from app.config import settings
from app.models import JobStatus, LifecycleState, Product, StrategyBrief
from app.modules.strategy import brand as brand_mod
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


def _make_product_with_brief(session, *, budget=0):
    product = Product(
        name="Auto Author",
        slug="auto-author",
        repo_local_path="/tmp/x",
        description="AI book-writing tool",
        token_budget_cents_month=budget,
        lifecycle_state=LifecycleState.STRATEGY,  # S1.1 already ran
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    brief = StrategyBrief(
        product_id=product.id,
        icp_json="{}",
        pain_points_json="[]",
        positioning="The fastest way to a finished manuscript.",
        channel_plan_json="[]",
        content_pillars_json='["craft", "publishing", "marketing"]',
        cadence_json="{}",
    )
    session.add(brief)
    session.commit()
    return product


def _stub_kit():
    return BrandKit(
        name="Auto Author",
        tone="encouraging and pragmatic",
        voice_descriptors=[
            VoiceDescriptor(descriptor="confident", guidance="state benefits plainly, no hedging")
        ],
        visual_seeds=["warm paper tones", "serif headlines"],
    )


# ---- schema --------------------------------------------------------------------------------


def test_brand_kit_schema_structures_voice_descriptors():
    kit = _stub_kit()
    assert kit.voice_descriptors[0].descriptor == "confident"
    assert kit.voice_descriptors[0].guidance  # structured for S4.3/S4.4 reuse


# ---- persistence + state -------------------------------------------------------------------


def test_generate_persists_brand_and_keeps_state(session):
    product = _make_product_with_brief(session)
    job = enqueue(session, "brand_kit", product_id=product.id)

    cost = brand_mod.generate_product_brand_kit(
        job, session, generate=lambda p, b, r: (_stub_kit(), 12)
    )

    assert cost == 12
    session.commit()  # the worker commits after the handler; mimic that before asserting
    session.refresh(product)
    kit = json.loads(product.brand_json)
    assert kit["name"] == "Auto Author"
    assert kit["voice_descriptors"][0]["descriptor"] == "confident"
    assert kit["visual_seeds"]  # visual seeds persisted
    assert product.lifecycle_state == LifecycleState.STRATEGY  # brand kit doesn't change state


def test_no_brief_raises(session):
    product = Product(name="No Brief", slug="no-brief", token_budget_cents_month=0)
    session.add(product)
    session.commit()
    session.refresh(product)
    job = enqueue(session, "brand_kit", product_id=product.id)

    with pytest.raises(RuntimeError, match="no strategy brief"):
        brand_mod.generate_product_brand_kit(
            job, session, generate=lambda p, b, r: (_stub_kit(), 1)
        )


# ---- budget gate ---------------------------------------------------------------------------


def test_budget_exceeded_raises_before_generate(session):
    product = _make_product_with_brief(session, budget=100)
    spent = enqueue(session, "brand_kit", product_id=product.id)
    spent.token_cost_cents = 100
    session.add(spent)
    session.commit()

    job = enqueue(session, "brand_kit", product_id=product.id)

    def _boom(_p, _b, _r):  # must not be reached
        raise AssertionError("generate called despite over-budget")

    with pytest.raises(RuntimeError, match="over monthly token budget"):
        brand_mod.generate_product_brand_kit(job, session, generate=_boom)


def test_zero_budget_is_unlimited(session):
    product = _make_product_with_brief(session, budget=0)
    job = enqueue(session, "brand_kit", product_id=product.id)
    captured = {}

    def _capture(p, b, r):
        captured["remaining"] = r
        return _stub_kit(), 1

    brand_mod.generate_product_brand_kit(job, session, generate=_capture)
    assert captured["remaining"] is None


def test_remaining_budget_is_capped_to_unspent(session):
    product = _make_product_with_brief(session, budget=100)
    spent = enqueue(session, "brand_kit", product_id=product.id)
    spent.token_cost_cents = 40
    session.add(spent)
    session.commit()
    job = enqueue(session, "brand_kit", product_id=product.id)

    captured = {}

    def _capture(p, b, r):
        captured["remaining"] = r
        return _stub_kit(), 1

    brand_mod.generate_product_brand_kit(job, session, generate=_capture)
    assert captured["remaining"] == 60  # 100 cap − 40 already spent this month


def test_real_generate_reserves_budget_for_synthesis(session, monkeypatch):
    product = _make_product_with_brief(session, budget=0)
    brief = session.exec(select(StrategyBrief).where(StrategyBrief.product_id == product.id)).one()

    monkeypatch.setattr(brand_mod, "build_client", lambda: object())

    def _no_call(*a, **k):
        raise AssertionError("generate_brand_kit must not run when it can't be afforded")

    monkeypatch.setattr(brand_mod, "generate_brand_kit", _no_call)

    # remaining 3; the reserved Opus cost (≥6¢ for 2000 output tokens) pushes over 3
    with pytest.raises(RuntimeError, match="reserve for brand kit"):
        brand_mod._real_generate(product, brief, 3)


# ---- worker path ---------------------------------------------------------------------------


def test_worker_runs_handler_and_records_cost(session, monkeypatch):
    product = _make_product_with_brief(session)
    monkeypatch.setattr(brand_mod, "_GENERATE", lambda p, b, r: (_stub_kit(), 77))
    job = enqueue(session, "brand_kit", product_id=product.id)

    assert "brand_kit" in worker._HANDLERS  # registered at import
    run_due_jobs(session)

    session.refresh(job)
    assert job.status == JobStatus.DONE
    assert job.token_cost_cents == 77  # cost recorded to job_run


# ---- API route -----------------------------------------------------------------------------


def test_route_enqueues_brand_job(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app

    product = _make_product_with_brief(session)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        resp = client.post(f"/api/private/strategy/{product.id}/brand")
    assert resp.status_code == 202
    assert resp.json()["status"] == JobStatus.QUEUED


def test_route_404_for_missing_product(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        resp = client.post("/api/private/strategy/999/brand")
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
        resp = client.post(f"/api/private/strategy/{product.id}/brand")
    assert resp.status_code == 400


# ---- real-API integration (key-gated) ------------------------------------------------------


@pytest.mark.skipif(
    settings.anthropic_api_key is None,
    reason="requires SME_ANTHROPIC_API_KEY (real API call); set it in the env or backend/.env",
)
def test_integration_real_brand_kit(session):
    product = _make_product_with_brief(session)
    job = enqueue(session, "brand_kit", product_id=product.id)

    cost = brand_mod.generate_product_brand_kit(job, session, generate=brand_mod._real_generate)

    assert cost > 0  # real token spend recorded
    session.commit()
    session.refresh(product)
    kit = json.loads(product.brand_json)
    assert kit["name"]  # non-empty brand name
    assert len(kit["voice_descriptors"]) >= 1  # at least one structured voice descriptor
