"""S2.7: pre-QA funnel smoke test.

A product in `setup_done` is auto-verified before the human QA gate: the built site exists and
wires the funnel contract, the four funnel events fire, and Checkout hits the correct test price. A
full pass advances it to `qa`; any failure keeps it in `setup_done`. The synthetic traffic the test
drives must never pollute the product's real funnel/revenue tables.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from app import config, workspace
from app.ai.client import BrandKit, SiteContent, VoiceDescriptor
from app.db import get_session
from app.main import create_app
from app.models import LifecycleState, MonetizationModel, Product
from app.models.funnel_event import FunnelEvent
from app.models.metric_event import MetricEvent
from app.modules.setup import site as site_mod


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
    monkeypatch.setattr(workspace.settings, "workspace_root", str(tmp_path / "ws"))
    monkeypatch.setattr(site_mod.settings, "public_api_base_url", "https://api.test")

    def _session_override():
        with Session(engine) as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _session_override
    yield app, engine


def _stub_content() -> SiteContent:
    return SiteContent(
        headline="Finish your book",
        subhead="From outline to manuscript, fast.",
        value_props=["AI drafts chapters", "Keeps your voice"],
        cta_label="Start free",
        primary_color="#1d4ed8",
        accent_color="#f59e0b",
        font_family="Georgia, serif",
    )


def _stub_kit() -> BrandKit:
    return BrandKit(
        name="Auto Author",
        tone="encouraging",
        voice_descriptors=[
            VoiceDescriptor(descriptor="confident", guidance="state benefits plainly")
        ],
        visual_seeds=["warm paper tones"],
    )


def _smoke_json(engine, product_id: int) -> str | None:
    with Session(engine) as s:
        return s.get(Product, product_id).smoke_test_json


def _seed(
    engine,
    *,
    state=LifecycleState.SETUP_DONE,
    price=2900,
    stripe_price_id="price_smoke",
    domain="autoauthor.app",
    build=True,
) -> int:
    with Session(engine) as s:
        product = Product(
            name="Auto Author",
            slug="auto-author",
            monetization_model=MonetizationModel.CC_SUB,
            price_amount_cents=price,
            price_interval="month",
            stripe_price_id=stripe_price_id,
            marketing_domain=domain,
            brand_json=_stub_kit().model_dump_json(),
            lifecycle_state=state,
        )
        s.add(product)
        s.commit()
        s.refresh(product)
        if build:
            # Build the real artifact the smoke test inspects (build already ran during setup).
            site_mod.build_site(product, _stub_content())
        return product.id


def _state(engine, product_id: int) -> LifecycleState:
    with Session(engine) as s:
        return s.get(Product, product_id).lifecycle_state


def _real_funnel_rows(engine) -> tuple[int, int]:
    """(funnel_events, metric_events) in the *real* DB — must stay empty after a smoke test."""
    with Session(engine) as s:
        return (
            len(s.exec(select(FunnelEvent)).all()),
            len(s.exec(select(MetricEvent)).all()),
        )


def test_pass_records_verdict_without_crossing_gate_or_polluting_real_metrics(ctx):
    app, engine = ctx
    product_id = _seed(engine)

    with TestClient(app) as client:
        resp = client.post(f"/api/private/qa/{product_id}/smoke-test")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is True
    stages = {s["stage"]: s["ok"] for s in body["stages"]}
    assert stages == {
        "build": True,
        "impression": True,
        "visit": True,
        "signup": True,
        "checkout": True,
        "paid": True,
    }
    # A pass records the verdict but does NOT cross the gate on its own — emitting the launch
    # checklist (S2.8) is what advances setup_done → qa.
    assert _state(engine, product_id) == LifecycleState.SETUP_DONE
    # Result folded onto the product for the dashboard.
    with Session(engine) as s:
        assert s.get(Product, product_id).smoke_test_json is not None
    # Synthetic traffic stayed in the throwaway DB — real funnel/revenue tables untouched.
    assert _real_funnel_rows(engine) == (0, 0)


def test_missing_site_artifact_keeps_setup_done(ctx):
    app, engine = ctx
    product_id = _seed(engine, build=False)

    with TestClient(app) as client:
        resp = client.post(f"/api/private/qa/{product_id}/smoke-test")

    assert resp.status_code == 200
    body = resp.json()
    assert body["passed"] is False
    stages = {s["stage"]: s["ok"] for s in body["stages"]}
    assert stages["build"] is False
    assert stages["impression"] is False  # nothing to inspect
    assert _state(engine, product_id) == LifecycleState.SETUP_DONE


def test_missing_stripe_price_fails_checkout_keeps_setup_done(ctx):
    app, engine = ctx
    product_id = _seed(engine, stripe_price_id=None)

    with TestClient(app) as client:
        resp = client.post(f"/api/private/qa/{product_id}/smoke-test")

    assert resp.status_code == 200
    body = resp.json()
    assert body["passed"] is False
    stages = {s["stage"]: s["ok"] for s in body["stages"]}
    assert stages["checkout"] is False
    assert _state(engine, product_id) == LifecycleState.SETUP_DONE
    # The failed verdict is still persisted for the dashboard, and the synthetic visit/signup
    # traffic this case drives before aborting at checkout must not leak into the real tables.
    assert _smoke_json(engine, product_id) is not None
    assert _real_funnel_rows(engine) == (0, 0)


def test_missing_price_amount_fails_paid_keeps_setup_done(ctx):
    # Stripe price configured (checkout passes) but no price_amount_cents → paid can't verify a
    # correct amount; it must fail rather than fake a $0 sale.
    app, engine = ctx
    product_id = _seed(engine, price=None)

    with TestClient(app) as client:
        resp = client.post(f"/api/private/qa/{product_id}/smoke-test")

    assert resp.status_code == 200
    body = resp.json()
    stages = {s["stage"]: s["ok"] for s in body["stages"]}
    assert stages["checkout"] is True  # earlier stages still pass
    assert stages["paid"] is False
    assert body["passed"] is False
    assert _state(engine, product_id) == LifecycleState.SETUP_DONE


def test_template_missing_funnel_hook_fails_impression(ctx):
    app, engine = ctx
    product_id = _seed(engine)
    # Corrupt the built artifact: present and non-empty (build ok) but missing the funnel wiring.
    index = workspace.workspace_path("auto-author") / "site" / "index.html"
    index.write_text("<html><body>no funnel here</body></html>", encoding="utf-8")

    with TestClient(app) as client:
        resp = client.post(f"/api/private/qa/{product_id}/smoke-test")

    body = resp.json()
    stages = {s["stage"]: s["ok"] for s in body["stages"]}
    assert stages["build"] is True
    assert stages["impression"] is False
    assert body["passed"] is False
    assert _state(engine, product_id) == LifecycleState.SETUP_DONE


def test_wrong_state_rejected(ctx):
    app, engine = ctx
    product_id = _seed(engine, state=LifecycleState.SETUP_READY)

    with TestClient(app) as client:
        resp = client.post(f"/api/private/qa/{product_id}/smoke-test")

    assert resp.status_code == 409
    assert _state(engine, product_id) == LifecycleState.SETUP_READY


def test_unknown_product_404(ctx):
    app, _ = ctx
    with TestClient(app) as client:
        resp = client.post("/api/private/qa/999/smoke-test")
    assert resp.status_code == 404
