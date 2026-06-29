"""S2.1: Templated landing site + funnel contract + UTM.

Deterministic unit tests drive the render/build/deploy + worker wiring + budget gate with no
network (the AI copy call is injected, exactly like the S1.2 brand tests). The integration test
makes a real Anthropic call and is skipped unless SME_ANTHROPIC_API_KEY is set.
"""

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app import worker
from app.ai.client import BrandKit, SiteContent, VoiceDescriptor
from app.config import settings
from app.models import JobStatus, LifecycleState, Product, StrategyBrief
from app.modules.setup import site as site_mod
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


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Isolate workspace + nginx roots so build/deploy hit tmp dirs, not the repo."""
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "ws"))
    monkeypatch.setattr(settings, "nginx_sites_root", str(tmp_path / "nginx"))
    monkeypatch.setattr(settings, "public_api_base_url", "https://api.example.com")
    return tmp_path


def _make_product(session, *, budget=0, brand=True, domain="autoauthor.app", state=None):
    product = Product(
        name="Auto Author",
        slug="auto-author",
        description="AI book-writing tool",
        token_budget_cents_month=budget,
        marketing_domain=domain,
        brand_json=_stub_kit().model_dump_json() if brand else None,
        lifecycle_state=state or LifecycleState.SETUP_READY,
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
        content_pillars_json="[]",
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


def _stub_content(**overrides):
    base = dict(
        headline="Finish your book",
        subhead="From outline to manuscript, fast.",
        value_props=["AI drafts chapters", "Keeps your voice", "Export anywhere"],
        cta_label="Start free",
        primary_color="#1d4ed8",
        accent_color="#f59e0b",
        font_family="Georgia, serif",
    )
    base.update(overrides)
    return SiteContent(**base)


# ---- schema --------------------------------------------------------------------------------


def test_site_content_rejects_non_hex_color():
    with pytest.raises(ValueError):
        _stub_content(primary_color="blue")


def test_site_content_rejects_css_injection_in_font():
    # font_family lands verbatim in a <style> block — a CSS-delimiter payload must be rejected.
    with pytest.raises(ValueError):
        _stub_content(font_family="serif} body{display:none} .x{")


# ---- render: funnel contract + brand tokens ------------------------------------------------


def test_render_wires_all_four_contract_components():
    html = site_mod.render_site("auto-author", _stub_content(), api_base_url="https://api.x.com")

    # AnalyticsSnippet (visit), EmailCapture (lead), StripeCheckout (checkout) hit the public funnel
    assert '"https://api.x.com"' in html  # api base injected as a JS string literal
    assert '"auto-author"' in html  # slug injected as a JS string literal
    assert "/visit" in html and "/lead" in html and "/checkout" in html
    assert 'id="email-capture"' in html  # EmailCapture form
    assert 'id="checkout"' in html  # StripeCheckout button
    # UTM capture → first-touch cookie + client_reference_id carried to checkout
    assert "first_touch_token" in html
    assert "client_reference_id" in html


def test_render_injects_brand_tokens_and_copy():
    html = site_mod.render_site("auto-author", _stub_content(), api_base_url="https://api.x.com")
    assert "--primary: #1d4ed8" in html
    assert "--accent: #f59e0b" in html
    assert "Georgia, serif" in html
    assert "Finish your book" in html
    assert "AI drafts chapters" in html


def test_render_autoescapes_ai_copy():
    """AI/owner copy is a trust boundary — a script payload in a slot must not render live."""
    html = site_mod.render_site(
        "auto-author",
        _stub_content(headline="<script>alert(1)</script>Boost"),
        api_base_url="https://api.x.com",
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ---- build (static export) + deploy (nginx) ------------------------------------------------


def test_build_writes_static_index_to_workspace(session, workspace):
    product = _make_product(session)
    site_dir = site_mod.build_site(product, _stub_content())
    index = site_dir / "index.html"
    assert index.is_file()
    assert "https://api.example.com" in index.read_text()  # uses configured public API base


def test_deploy_places_site_and_emits_vhost(session, workspace):
    product = _make_product(session)
    site_dir = site_mod.build_site(product, _stub_content())
    dest = site_mod.deploy_site(product, site_dir)

    assert (dest / "index.html").is_file()
    assert dest.name == "autoauthor.app"  # keyed by marketing_domain
    vhost = (dest.parent / "autoauthor.app.conf").read_text()
    assert "server_name autoauthor.app;" in vhost
    assert str(dest) in vhost  # root points at the deployed dir


def test_deploy_is_idempotent(session, workspace):
    product = _make_product(session)
    site_dir = site_mod.build_site(product, _stub_content())
    site_mod.deploy_site(product, site_dir)
    dest = site_mod.deploy_site(product, site_dir)  # second run replaces wholesale, no error
    assert (dest / "index.html").is_file()


def test_deploy_requires_marketing_domain(session, workspace):
    product = _make_product(session, domain=None)
    site_dir = site_mod.build_site(product, _stub_content())
    with pytest.raises(RuntimeError, match="no marketing_domain"):
        site_mod.deploy_site(product, site_dir)


@pytest.mark.parametrize(
    "evil", ["../../etc", "/etc/nginx", "a/b", "foo;rm -rf", "..", "localhost"]
)
def test_deploy_rejects_non_hostname_domain(session, workspace, evil):
    """A path-traversal / metacharacter domain must not reach rmtree/copytree or the vhost."""
    product = _make_product(session, domain=evil)
    site_dir = site_mod.build_site(product, _stub_content())
    with pytest.raises(RuntimeError, match="not a valid hostname"):
        site_mod.deploy_site(product, site_dir)
    # nothing escaped the configured root
    assert not (workspace / "nginx").exists() or not any((workspace / "nginx").iterdir())


# ---- handler: persistence-free, returns cost -----------------------------------------------


def test_build_product_site_renders_deploys_and_returns_cost(session, workspace):
    product = _make_product(session)
    job = enqueue(session, "setup_site", product_id=product.id)

    cost = site_mod.build_product_site(
        job, session, generate=lambda p, k, pos, r: (_stub_content(), 9)
    )

    assert cost == 9
    deployed = workspace / "nginx" / "autoauthor.app" / "index.html"
    assert deployed.is_file()
    assert "Finish your book" in deployed.read_text()


def test_build_product_site_passes_positioning_to_generate(session, workspace):
    product = _make_product(session)
    job = enqueue(session, "setup_site", product_id=product.id)
    captured = {}

    def _capture(p, kit, positioning, remaining):
        captured["positioning"] = positioning
        captured["kit_name"] = kit.name
        return _stub_content(), 1

    site_mod.build_product_site(job, session, generate=_capture)
    assert captured["positioning"] == "The fastest way to a finished manuscript."
    assert captured["kit_name"] == "Auto Author"  # brand_json parsed back to a BrandKit


def test_no_brand_kit_raises(session, workspace):
    product = _make_product(session, brand=False)
    job = enqueue(session, "setup_site", product_id=product.id)
    with pytest.raises(RuntimeError, match="no brand kit"):
        site_mod.build_product_site(
            job, session, generate=lambda p, k, pos, r: (_stub_content(), 1)
        )


# ---- budget gate ---------------------------------------------------------------------------


def test_budget_exceeded_raises_before_generate(session, workspace):
    product = _make_product(session, budget=100)
    spent = enqueue(session, "setup_site", product_id=product.id)
    spent.token_cost_cents = 100
    session.add(spent)
    session.commit()
    job = enqueue(session, "setup_site", product_id=product.id)

    def _boom(*_a):
        raise AssertionError("generate called despite over-budget")

    with pytest.raises(RuntimeError, match="over monthly token budget"):
        site_mod.build_product_site(job, session, generate=_boom)


def test_zero_budget_is_unlimited(session, workspace):
    product = _make_product(session, budget=0)
    job = enqueue(session, "setup_site", product_id=product.id)
    captured = {}

    def _capture(p, k, pos, r):
        captured["remaining"] = r
        return _stub_content(), 1

    site_mod.build_product_site(job, session, generate=_capture)
    assert captured["remaining"] is None


def test_remaining_budget_is_capped_to_unspent(session, workspace):
    product = _make_product(session, budget=100)
    spent = enqueue(session, "setup_site", product_id=product.id)
    spent.token_cost_cents = 40
    session.add(spent)
    session.commit()
    job = enqueue(session, "setup_site", product_id=product.id)
    captured = {}

    def _capture(p, k, pos, r):
        captured["remaining"] = r
        return _stub_content(), 1

    site_mod.build_product_site(job, session, generate=_capture)
    assert captured["remaining"] == 60  # 100 cap − 40 already spent this month


def test_real_generate_reserves_budget_for_synthesis(session, workspace, monkeypatch):
    product = _make_product(session, budget=0)
    monkeypatch.setattr(site_mod, "build_client", lambda: object())

    def _no_call(*a, **k):
        raise AssertionError("generate_site_content must not run when it can't be afforded")

    monkeypatch.setattr(site_mod, "generate_site_content", _no_call)

    with pytest.raises(RuntimeError, match="reserve for site content"):
        site_mod._real_generate(product, _stub_kit(), "positioning", 2)


# ---- worker path ---------------------------------------------------------------------------


def test_worker_runs_handler_and_records_cost(session, workspace, monkeypatch):
    product = _make_product(session)
    monkeypatch.setattr(site_mod, "_GENERATE", lambda p, k, pos, r: (_stub_content(), 55))
    job = enqueue(session, "setup_site", product_id=product.id)

    assert "setup_site" in worker._HANDLERS  # registered at import
    run_due_jobs(session)

    session.refresh(job)
    assert job.status == JobStatus.DONE
    assert job.token_cost_cents == 55


# ---- API route -----------------------------------------------------------------------------


def _client(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_route_enqueues_site_job(session):
    product = _make_product(session)
    with _client(session) as client:
        resp = client.post(f"/api/private/setup/{product.id}/site")
    assert resp.status_code == 202
    assert resp.json()["status"] == JobStatus.QUEUED


def test_route_404_for_missing_product(session):
    with _client(session) as client:
        resp = client.post("/api/private/setup/999/site")
    assert resp.status_code == 404


def test_route_409_when_not_setup_ready(session):
    product = _make_product(session, state=LifecycleState.STRATEGY)
    with _client(session) as client:
        resp = client.post(f"/api/private/setup/{product.id}/site")
    assert resp.status_code == 409


def test_route_400_when_no_brand_kit(session):
    product = _make_product(session, brand=False)
    with _client(session) as client:
        resp = client.post(f"/api/private/setup/{product.id}/site")
    assert resp.status_code == 400


# ---- real-API integration (key-gated) ------------------------------------------------------


@pytest.mark.skipif(
    settings.anthropic_api_key is None,
    reason="requires SME_ANTHROPIC_API_KEY (real API call); set it in the env or backend/.env",
)
def test_integration_real_site_content(session, workspace):
    product = _make_product(session)
    job = enqueue(session, "setup_site", product_id=product.id)

    cost = site_mod.build_product_site(job, session, generate=site_mod._real_generate)

    assert cost > 0  # real token spend recorded
    deployed = workspace / "nginx" / "autoauthor.app" / "index.html"
    html = deployed.read_text()
    assert "/visit" in html and "/lead" in html  # contract plumbing present on the real site
