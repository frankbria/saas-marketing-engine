"""S2.8: launch checklist emission.

A product that has *passed* the pre-QA smoke test (S2.7) gets a launch checklist emitted from its
real setup output (smoke verdict, channels prepared, human-setup punch-list, Stripe config).
Emitting the checklist is what crosses `setup_done → qa` (TECH_SPEC line 112: smoke pass + checklist
emitted). A product whose smoke test is missing or failed never crosses — broken plumbing stays in
`setup_done`. Incomplete human-setup items are surfaced on the checklist but do not block the gate.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app import config
from app.db import get_session
from app.main import create_app
from app.models import (
    Channel,
    ChannelType,
    LifecycleState,
    MonetizationModel,
    Product,
    SetupChecklistItem,
    SetupItemStatus,
)
from app.modules.qa.smoke_test import SmokeTestResult, StageResult

_STAGES = ("build", "impression", "visit", "signup", "checkout", "paid")


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(config.settings, "workspace_root", str(tmp_path / "ws"))

    def _session_override():
        with Session(engine) as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _session_override
    yield app, engine


def _smoke_json(*, passed: bool) -> str:
    return SmokeTestResult(
        passed=passed,
        ran_at=datetime.now(UTC),
        stages=[StageResult(stage=s, ok=passed) for s in _STAGES],
    ).model_dump_json()


def _seed(
    engine,
    *,
    state=LifecycleState.SETUP_DONE,
    smoke="pass",  # "pass" | "fail" | None
    channels=("blog", "reddit"),
    pending_setup=0,
    done_setup=0,
) -> int:
    with Session(engine) as s:
        product = Product(
            name="Auto Author",
            slug="auto-author",
            monetization_model=MonetizationModel.CC_SUB,
            price_amount_cents=2900,
            price_interval="month",
            stripe_price_id="price_smoke",
            marketing_domain="autoauthor.app",
            lifecycle_state=state,
            smoke_test_json=None if smoke is None else _smoke_json(passed=smoke == "pass"),
        )
        s.add(product)
        s.commit()
        s.refresh(product)
        for ct in channels:
            s.add(Channel(product_id=product.id, type=ChannelType(ct), enabled=True))
        ord_ = 1
        for _ in range(done_setup):
            s.add(
                SetupChecklistItem(
                    product_id=product.id,
                    ord=ord_,
                    instruction=f"done {ord_}",
                    category="account",
                    status=SetupItemStatus.DONE,
                )
            )
            ord_ += 1
        for _ in range(pending_setup):
            s.add(
                SetupChecklistItem(
                    product_id=product.id,
                    ord=ord_,
                    instruction=f"connect oauth {ord_}",
                    category="oauth",
                    status=SetupItemStatus.PENDING,
                )
            )
            ord_ += 1
        s.commit()
        return product.id


def _state(engine, product_id: int) -> LifecycleState:
    with Session(engine) as s:
        return s.get(Product, product_id).lifecycle_state


def test_emit_advances_to_qa_and_stores_checklist(ctx):
    app, engine = ctx
    product_id = _seed(engine, channels=("blog", "reddit"), done_setup=2)

    with TestClient(app) as client:
        resp = client.post(f"/api/private/qa/{product_id}/launch-checklist")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"], "checklist must have items derived from setup state"
    labels = {i["label"] for i in body["items"]}
    # Checklist is built from real setup output, not a static template.
    assert any("smoke" in label.lower() for label in labels)
    assert any("channel" in label.lower() for label in labels)
    assert all({"ord", "label", "detail", "ready"} <= i.keys() for i in body["items"])

    # Gate crossed.
    assert _state(engine, product_id) == LifecycleState.QA
    # Folded onto the product for the dashboard.
    with Session(engine) as s:
        assert s.get(Product, product_id).launch_checklist_json is not None


def test_checklist_reflects_real_setup_state(ctx):
    app, engine = ctx
    # 1 done, 2 pending human-setup steps; one channel prepared.
    product_id = _seed(engine, channels=("blog",), done_setup=1, pending_setup=2)

    with TestClient(app) as client:
        resp = client.post(f"/api/private/qa/{product_id}/launch-checklist")

    body = resp.json()
    items = {i["label"]: i for i in body["items"]}
    human = next(i for k, i in items.items() if "human setup" in k.lower())
    assert human["ready"] is False  # 2 pending → not complete
    assert "pending" in human["detail"].lower()
    chan = next(i for k, i in items.items() if "channel" in k.lower())
    assert chan["ready"] is True and "blog" in chan["detail"]
    # Pending human steps surface but do NOT block the gate.
    assert _state(engine, product_id) == LifecycleState.QA


def test_smoke_failed_blocks_gate(ctx):
    app, engine = ctx
    product_id = _seed(engine, smoke="fail")

    with TestClient(app) as client:
        resp = client.post(f"/api/private/qa/{product_id}/launch-checklist")

    assert resp.status_code == 409
    assert _state(engine, product_id) == LifecycleState.SETUP_DONE


def test_smoke_not_run_blocks_gate(ctx):
    app, engine = ctx
    product_id = _seed(engine, smoke=None)

    with TestClient(app) as client:
        resp = client.post(f"/api/private/qa/{product_id}/launch-checklist")

    assert resp.status_code == 409
    assert _state(engine, product_id) == LifecycleState.SETUP_DONE


def test_corrupt_smoke_json_blocks_gate(ctx):
    # A non-null but unreadable stored smoke verdict must 409, not 500.
    app, engine = ctx
    product_id = _seed(engine)
    with Session(engine) as s:
        product = s.get(Product, product_id)
        product.smoke_test_json = "{not valid json"
        s.add(product)
        s.commit()

    with TestClient(app) as client:
        resp = client.post(f"/api/private/qa/{product_id}/launch-checklist")

    assert resp.status_code == 409
    assert _state(engine, product_id) == LifecycleState.SETUP_DONE


def test_wrong_state_rejected(ctx):
    app, engine = ctx
    product_id = _seed(engine, state=LifecycleState.SETUP_READY)

    with TestClient(app) as client:
        resp = client.post(f"/api/private/qa/{product_id}/launch-checklist")

    assert resp.status_code == 409
    assert _state(engine, product_id) == LifecycleState.SETUP_READY


def test_rerun_after_qa_rejected(ctx):
    app, engine = ctx
    product_id = _seed(engine)

    with TestClient(app) as client:
        first = client.post(f"/api/private/qa/{product_id}/launch-checklist")
        assert first.status_code == 200
        second = client.post(f"/api/private/qa/{product_id}/launch-checklist")

    assert second.status_code == 409  # already qa, not setup_done
    assert _state(engine, product_id) == LifecycleState.QA


def test_unknown_product_404(ctx):
    app, _ = ctx
    with TestClient(app) as client:
        resp = client.post("/api/private/qa/999/launch-checklist")
    assert resp.status_code == 404
