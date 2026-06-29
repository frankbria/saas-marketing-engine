"""S1.1: codebase ingest → Marketing Brief.

Deterministic unit tests drive the worker wiring, persistence, budget gate, ingest, and pricing
with no network. The integration test makes a real Anthropic call and is skipped unless
SME_ANTHROPIC_API_KEY is set (honors the no-mock rule without spending money in CI).
"""

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app import worker
from app.ai import pricing
from app.ai.client import ICP, BriefDraft, Cadence, ChannelPlanItem
from app.config import settings
from app.models import JobStatus, LifecycleState, Product, StrategyBrief
from app.modules.strategy import brief as brief_mod
from app.modules.strategy import ingest
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


def _make_product(session, *, budget=0, repo_local_path="/tmp/x"):
    product = Product(
        name="Auto Author",
        slug="auto-author",
        repo_local_path=repo_local_path,
        description="AI book-writing tool",
        token_budget_cents_month=budget,
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def _stub_brief():
    return BriefDraft(
        icp=ICP(segment="indie authors", description="self-publishers", firmographics=["solo"]),
        pain_points=["slow drafting", "blank page"],
        positioning="The fastest way to a finished manuscript.",
        channel_plan=[ChannelPlanItem(channel="blog", rationale="SEO", priority=1)],
        content_pillars=["craft", "publishing", "marketing"],
        cadence=Cadence(summary="3x/week", posts_per_week=3),
    )


# ---- pricing -------------------------------------------------------------------------------


def test_cost_cents_opus():
    # 1M in + 1M out at $5/$25 = $30 = 3000 cents
    assert pricing.cost_cents("claude-opus-4-8", 1_000_000, 1_000_000) == 3000


def test_cost_cents_rounds_up():
    # tiny usage still bills at least 1 cent (ceil), never 0
    assert pricing.cost_cents("claude-haiku-4-5", 100, 100) == 1


def test_cost_cents_unknown_model_raises():
    with pytest.raises(KeyError):
        pricing.cost_cents("gpt-4", 1, 1)


# ---- ingest --------------------------------------------------------------------------------


def _build_repo(root: Path):
    (root / "README.md").write_text("# Auto Author\nWrite books with AI.")
    (root / "pyproject.toml").write_text("[project]\nname='auto-author'")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("How to use Auto Author.")
    (root / "src" / "api").mkdir(parents=True)
    (root / "src" / "api" / "routes.py").write_text("@app.get('/draft')\ndef draft(): ...")
    (root / "src" / "components").mkdir(parents=True)
    (root / "src" / "components" / "Hero.tsx").write_text("<h1>Write your book faster</h1>")
    (root / "src" / "helpers.py").write_text("def util(): ...")  # not route/UI/manifest/doc
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("var x = 1;")


def test_collect_signal_files_picks_signal_skips_noise(tmp_path):
    _build_repo(tmp_path)
    files = ingest.collect_signal_files(tmp_path)
    rels = [r for r, _ in files]

    assert rels[0] == "README.md"  # README is highest priority
    assert "pyproject.toml" in rels
    assert os.path.join("docs", "guide.md") in rels
    assert os.path.join("src", "api", "routes.py") in rels  # route-hinted source
    assert os.path.join("src", "components", "Hero.tsx") in rels  # UI copy source (§5)
    assert not any("node_modules" in r for r in rels)  # noise dir skipped
    assert not any(r.endswith("helpers.py") for r in rels)  # non-signal source skipped


def test_collect_signal_files_skips_dot_directories(tmp_path):
    (tmp_path / "README.md").write_text("real")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "README.md").write_text("cache noise")
    rels = [r for r, _ in ingest.collect_signal_files(tmp_path)]
    assert rels == ["README.md"]  # dot-dir cache README excluded


def test_collect_signal_files_excludes_symlinks_escaping_repo(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("real readme")
    (repo / "EVIL.md").symlink_to(secret)  # signal name, points outside the repo

    rels = [r for r, _ in ingest.collect_signal_files(repo)]
    assert "README.md" in rels
    assert "EVIL.md" not in rels  # symlink escaping the repo is never read


def test_collect_signal_files_truncates_large_files(tmp_path):
    (tmp_path / "README.md").write_text("x" * (ingest.MAX_BYTES + 5000))
    [(_, text)] = ingest.collect_signal_files(tmp_path)
    assert len(text) == ingest.MAX_BYTES


def test_resolve_repo_local_path(tmp_path):
    assert ingest.resolve_repo(str(tmp_path), None, tmp_path / "clone") == tmp_path


def test_resolve_repo_missing_local_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest.resolve_repo(str(tmp_path / "nope"), None, tmp_path / "clone")


def test_resolve_repo_no_source_raises(tmp_path):
    with pytest.raises(ValueError):
        ingest.resolve_repo(None, None, tmp_path / "clone")


# ---- budget gate ---------------------------------------------------------------------------


def test_month_to_date_cost_sums_this_months_jobs(session):
    product = _make_product(session)
    j = enqueue(session, "strategy_brief", product_id=product.id)
    j.token_cost_cents = 150
    session.add(j)
    session.commit()
    from app.modules.strategy.brief import _utcnow, month_to_date_cost_cents

    assert month_to_date_cost_cents(session, product.id, _utcnow()) == 150


def test_budget_exceeded_raises_before_generate(session):
    product = _make_product(session, budget=100)
    spent = enqueue(session, "strategy_brief", product_id=product.id)
    spent.token_cost_cents = 100
    session.add(spent)
    session.commit()

    job = enqueue(session, "strategy_brief", product_id=product.id)

    def _boom(_p, _s, _r):  # must not be reached
        raise AssertionError("generate called despite over-budget")

    with pytest.raises(RuntimeError, match="over monthly token budget"):
        brief_mod.generate_strategy_brief(job, session, generate=_boom)


def test_zero_budget_is_unlimited(session):
    product = _make_product(session, budget=0)
    job = enqueue(session, "strategy_brief", product_id=product.id)
    brief_mod.generate_strategy_brief(
        job, session, generate=lambda p, s, r: (_stub_brief(), 7, "{}")
    )
    session.commit()  # the worker commits after the handler; mimic that before asserting
    session.refresh(product)
    assert product.lifecycle_state == LifecycleState.STRATEGY
    # 0-budget passes remaining=None (unlimited) to generate
    captured = {}

    def _capture(p, s, r):
        captured["remaining"] = r
        return _stub_brief(), 1, "{}"

    brief_mod.generate_strategy_brief(job, session, generate=_capture)
    assert captured["remaining"] is None


def test_remaining_budget_is_capped_to_unspent(session):
    product = _make_product(session, budget=100)
    spent = enqueue(session, "strategy_brief", product_id=product.id)
    spent.token_cost_cents = 40
    session.add(spent)
    session.commit()
    job = enqueue(session, "strategy_brief", product_id=product.id)

    captured = {}

    def _capture(p, s, r):
        captured["remaining"] = r
        return _stub_brief(), 1, "{}"

    brief_mod.generate_strategy_brief(job, session, generate=_capture)
    assert captured["remaining"] == 60  # 100 cap − 40 already spent this month


def test_real_generate_stops_before_synthesis_when_over_budget(tmp_path, monkeypatch):
    _build_repo(tmp_path)
    product = Product(name="x", slug="x", repo_local_path=str(tmp_path), token_budget_cents_month=0)
    product.id = 1

    monkeypatch.setattr(brief_mod, "build_client", lambda: object())
    monkeypatch.setattr(brief_mod, "summarize_file", lambda client, rel, content: ("summary", 30))

    def _no_synthesis(*a, **k):
        raise AssertionError("synthesis must not run once budget is exhausted")

    monkeypatch.setattr(brief_mod, "synthesize_brief", _no_synthesis)

    # remaining=50; two 30-cent summaries reach 60 ≥ 50 → abort before the opus synthesis call
    with pytest.raises(RuntimeError, match="budget exhausted"):
        brief_mod._real_generate(product, None, 50)


def test_real_generate_reserves_budget_for_synthesis(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# x")  # single cheap file
    product = Product(name="x", slug="x", repo_local_path=str(repo), token_budget_cents_month=0)
    product.id = 1

    monkeypatch.setattr(brief_mod, "build_client", lambda: object())
    monkeypatch.setattr(brief_mod, "summarize_file", lambda client, rel, content: ("s", 2))

    def _no_synthesis(*a, **k):
        raise AssertionError("synthesis must not run when it can't be afforded")

    monkeypatch.setattr(brief_mod, "synthesize_brief", _no_synthesis)

    # summary cost 2, remaining 15; the reserved synthesis cost (≥20¢ Opus floor) pushes over 15
    with pytest.raises(RuntimeError, match="reserve for synthesis"):
        brief_mod._real_generate(product, None, 15)


# ---- persistence + worker path -------------------------------------------------------------


def test_generate_persists_brief_and_advances_state(session):
    product = _make_product(session)
    job = enqueue(session, "strategy_brief", product_id=product.id)

    cost = brief_mod.generate_strategy_brief(
        job, session, generate=lambda p, s, r: (_stub_brief(), 42, '{"raw": true}')
    )

    assert cost == 42
    session.commit()  # the worker commits after the handler; mimic that before asserting
    from sqlmodel import select

    row = session.exec(select(StrategyBrief)).first()
    assert row.product_id == product.id
    assert json.loads(row.content_pillars_json) == ["craft", "publishing", "marketing"]
    assert json.loads(row.icp_json)["segment"] == "indie authors"
    assert row.raw_ai_output == '{"raw": true}'
    session.refresh(product)
    assert product.lifecycle_state == LifecycleState.STRATEGY


def test_generate_is_idempotent_upsert(session):
    product = _make_product(session)
    job = enqueue(session, "strategy_brief", product_id=product.id)
    gen = lambda p, s, r: (_stub_brief(), 1, "{}")  # noqa: E731
    brief_mod.generate_strategy_brief(job, session, generate=gen)
    brief_mod.generate_strategy_brief(job, session, generate=gen)

    from sqlmodel import select

    rows = session.exec(select(StrategyBrief).where(StrategyBrief.product_id == product.id)).all()
    assert len(rows) == 1  # 1:1, second run updates rather than inserts


def test_worker_runs_handler_and_records_cost(session, monkeypatch):
    product = _make_product(session)
    monkeypatch.setattr(brief_mod, "_GENERATE", lambda p, s, r: (_stub_brief(), 99, "{}"))
    job = enqueue(session, "strategy_brief", product_id=product.id)

    assert "strategy_brief" in worker._HANDLERS  # registered at import
    run_due_jobs(session)

    session.refresh(job)
    assert job.status == JobStatus.DONE
    assert job.token_cost_cents == 99  # cost recorded to job_run


# ---- API route -----------------------------------------------------------------------------


def test_route_enqueues_job(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app

    product = _make_product(session, repo_local_path="/tmp/repo")
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        resp = client.post(f"/api/private/strategy/{product.id}/brief")
    assert resp.status_code == 202
    assert resp.json()["status"] == JobStatus.QUEUED


def test_route_404_for_missing_product(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        resp = client.post("/api/private/strategy/999/brief")
    assert resp.status_code == 404


def test_route_400_when_no_repo(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app

    product = Product(name="No Repo", slug="no-repo", token_budget_cents_month=0)
    session.add(product)
    session.commit()
    session.refresh(product)

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        resp = client.post(f"/api/private/strategy/{product.id}/brief")
    assert resp.status_code == 400


# ---- real-API integration (key-gated) ------------------------------------------------------


@pytest.mark.skipif(
    settings.anthropic_api_key is None,
    reason="requires SME_ANTHROPIC_API_KEY (real API call); set it in the env or backend/.env",
)
def test_integration_real_brief_on_fixture_repo(session, tmp_path):
    _build_repo(tmp_path)
    product = _make_product(session, repo_local_path=str(tmp_path))
    job = enqueue(session, "strategy_brief", product_id=product.id)

    cost = brief_mod.generate_strategy_brief(job, session, generate=brief_mod._real_generate)

    assert cost > 0  # real token spend recorded
    from sqlmodel import select

    row = session.exec(select(StrategyBrief).where(StrategyBrief.product_id == product.id)).one()
    assert json.loads(row.icp_json)["segment"]  # non-empty ICP
    assert len(json.loads(row.content_pillars_json)) >= 3  # ≥3 content pillars
