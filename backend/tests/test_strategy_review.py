"""S1.4: owner review/edit + approve strategy.

Covers the review GET, brief/brand/price edits, and the approve transition
(`strategy → setup_ready`) with its completeness + state guards. No network.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app import workspace
from app.db import get_session
from app.main import create_app
from app.models import LifecycleState, MonetizationModel, Product, StrategyBrief


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(workspace.settings, "workspace_root", str(tmp_path / "ws"))

    def _session_override():
        with Session(engine) as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _session_override
    with TestClient(app) as c:
        yield c, engine


def _seed(
    engine,
    *,
    slug="auto-author",
    state=LifecycleState.STRATEGY,
    with_brief=True,
    brand=True,
    price=True,
    model=MonetizationModel.CC_SUB,
):
    with Session(engine) as s:
        product = Product(
            name="Auto Author",
            slug=slug,
            repo_local_path="/tmp/x",
            monetization_model=model,
            lifecycle_state=state,
            brand_json='{"name": "Auto Author", "tone": "warm"}' if brand else None,
            price_amount_cents=2900 if price else None,
            price_interval="month" if price else None,
        )
        s.add(product)
        s.commit()
        s.refresh(product)
        if with_brief:
            s.add(
                StrategyBrief(
                    product_id=product.id,
                    icp_json='{"segment": "indie authors"}',
                    pain_points_json="[]",
                    positioning="The fastest way to a finished manuscript.",
                    channel_plan_json="[]",
                    content_pillars_json='["craft"]',
                    cadence_json="{}",
                )
            )
            s.commit()
        return product.id


# ---- review GET ----------------------------------------------------------------------------


def test_get_strategy_returns_brief(client):
    c, engine = client
    pid = _seed(engine)
    resp = c.get(f"/api/private/strategy/{pid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["positioning"] == "The fastest way to a finished manuscript."
    assert body["approved"] is False


def test_get_strategy_404_without_brief(client):
    c, engine = client
    pid = _seed(engine, with_brief=False)
    assert c.get(f"/api/private/strategy/{pid}").status_code == 404


# ---- brief edit ----------------------------------------------------------------------------


def test_patch_brief_edits_fields(client):
    c, engine = client
    pid = _seed(engine)
    resp = c.patch(
        f"/api/private/strategy/{pid}",
        json={"positioning": "edited", "content_pillars_json": '["a", "b"]'},
    )
    assert resp.status_code == 200
    assert resp.json()["positioning"] == "edited"
    assert c.get(f"/api/private/strategy/{pid}").json()["content_pillars_json"] == '["a", "b"]'


def test_patch_brief_rejects_malformed_json(client):
    c, engine = client
    pid = _seed(engine)
    resp = c.patch(f"/api/private/strategy/{pid}", json={"content_pillars_json": "{not json"})
    assert resp.status_code == 422


# ---- brand edit (via product PATCH) --------------------------------------------------------


def test_patch_product_brand_json(client):
    c, engine = client
    pid = _seed(engine)
    resp = c.patch(
        f"/api/private/products/{pid}", json={"brand_json": '{"name": "AA", "tone": "bold"}'}
    )
    assert resp.status_code == 200
    assert resp.json()["brand_json"] == '{"name": "AA", "tone": "bold"}'


def test_patch_product_rejects_malformed_brand_json(client):
    c, engine = client
    pid = _seed(engine)
    assert c.patch(f"/api/private/products/{pid}", json={"brand_json": "nope"}).status_code == 422


# ---- approve -------------------------------------------------------------------------------


def test_approve_transitions_to_setup_ready(client):
    c, engine = client
    pid = _seed(engine)
    resp = c.post(f"/api/private/strategy/{pid}/approve")
    assert resp.status_code == 200
    assert resp.json()["lifecycle_state"] == "setup_ready"
    brief = c.get(f"/api/private/strategy/{pid}").json()
    assert brief["approved"] is True
    assert brief["approved_at"] is not None


def test_approve_rejects_wrong_state(client):
    c, engine = client
    pid = _seed(engine, state=LifecycleState.DRAFT)
    assert c.post(f"/api/private/strategy/{pid}/approve").status_code == 409


def test_approve_rejects_incomplete_strategy(client):
    c, engine = client
    pid = _seed(engine, slug="no-brand", brand=False)  # brand missing
    assert c.post(f"/api/private/strategy/{pid}/approve").status_code == 400
    pid2 = _seed(engine, slug="no-price", price=False)  # cc_sub but no price
    assert c.post(f"/api/private/strategy/{pid2}/approve").status_code == 400


def test_approve_rejects_without_brief(client):
    c, engine = client
    pid = _seed(engine, with_brief=False)
    assert c.post(f"/api/private/strategy/{pid}/approve").status_code == 400


def test_approve_404_missing_product(client):
    c, _ = client
    assert c.post("/api/private/strategy/999/approve").status_code == 404
