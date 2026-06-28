"""S0.1 scaffold: the app boots and both API surfaces are wired."""

from fastapi.testclient import TestClient

from app.main import create_app

client = TestClient(create_app())


def test_root_health_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_private_router_mounted():
    resp = client.get("/api/private/health")
    assert resp.status_code == 200
    assert resp.json() == {"surface": "private", "status": "ok"}


def test_public_router_mounted():
    resp = client.get("/api/public/health")
    assert resp.status_code == 200
    assert resp.json() == {"surface": "public", "status": "ok"}
