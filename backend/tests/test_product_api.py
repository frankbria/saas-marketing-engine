"""S0.3: product CRUD API — roundtrip, workspace/vault creation, G7 (multi-product)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app import workspace
from app.db import get_session
from app.main import create_app


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
        yield c, tmp_path / "ws"


def test_create_product_persists_and_scaffolds(client):
    c, ws_root = client
    resp = c.post(
        "/api/private/products",
        json={
            "name": "Auto Author",
            "repo_url": "https://github.com/frankbria/auto-author",
            "description": "AI book writing",
            "marketing_domain": "autoauthor.app",
            "token_budget_cents_month": 10000,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["slug"] == "auto-author"
    assert body["lifecycle_state"] == "draft"
    assert body["monetization_model"] == "cc_sub"
    assert body["token_budget_cents_month"] == 10000
    # isolated workspace + empty credentials vault created on disk
    assert (ws_root / "auto-author" / "vault").is_dir()


def test_list_and_get(client):
    c, _ = client
    pid = c.post("/api/private/products", json={"name": "Widget"}).json()["id"]

    listed = c.get("/api/private/products").json()
    assert any(p["id"] == pid for p in listed)

    got = c.get(f"/api/private/products/{pid}")
    assert got.status_code == 200
    assert got.json()["slug"] == "widget"

    assert c.get("/api/private/products/99999").status_code == 404


def test_update_config_fields(client):
    c, _ = client
    pid = c.post("/api/private/products", json={"name": "Widget"}).json()["id"]
    resp = c.patch(
        f"/api/private/products/{pid}",
        json={"marketing_domain": "widget.app", "token_budget_cents_month": 2500},
    )
    assert resp.status_code == 200
    assert resp.json()["marketing_domain"] == "widget.app"
    assert resp.json()["token_budget_cents_month"] == 2500


def test_lifecycle_not_editable_via_patch(client):
    """lifecycle_state is a state machine, not raw-settable config (PATCH ignores it)."""
    c, _ = client
    pid = c.post("/api/private/products", json={"name": "Widget"}).json()["id"]
    resp = c.patch(f"/api/private/products/{pid}", json={"lifecycle_state": "live"})
    assert resp.status_code == 200
    assert resp.json()["lifecycle_state"] == "draft"


def test_delete_removes_workspace(client):
    c, ws_root = client
    pid = c.post("/api/private/products", json={"name": "Gone"}).json()["id"]
    assert (ws_root / "gone").is_dir()
    assert c.delete(f"/api/private/products/{pid}").status_code == 204
    assert c.get(f"/api/private/products/{pid}").status_code == 404
    assert not (ws_root / "gone").exists()


def test_two_products_no_hardcoding(client):
    """G7: a second product with different values runs through identically."""
    c, ws_root = client
    a = c.post("/api/private/products", json={"name": "Auto Author"}).json()
    b = c.post(
        "/api/private/products",
        json={"name": "Other SaaS", "marketing_domain": "other.app"},
    ).json()

    assert a["slug"] != b["slug"]
    assert (ws_root / a["slug"] / "vault").is_dir()
    assert (ws_root / b["slug"] / "vault").is_dir()


def test_duplicate_name_gets_unique_slug(client):
    c, _ = client
    s1 = c.post("/api/private/products", json={"name": "Same"}).json()["slug"]
    s2 = c.post("/api/private/products", json={"name": "Same"}).json()["slug"]
    assert s1 == "same"
    assert s2 == "same-2"


def test_blank_name_rejected(client):
    c, _ = client
    assert c.post("/api/private/products", json={"name": "   "}).status_code == 422


def test_negative_budget_rejected(client):
    c, _ = client
    resp = c.post("/api/private/products", json={"name": "Widget", "token_budget_cents_month": -5})
    assert resp.status_code == 422


def test_patch_bumps_updated_at(client):
    c, _ = client
    created = c.post("/api/private/products", json={"name": "Widget"}).json()
    patched = c.patch(
        f"/api/private/products/{created['id']}", json={"description": "now with words"}
    ).json()
    assert patched["updated_at"] >= created["updated_at"]
    assert patched["updated_at"] != created["updated_at"]
