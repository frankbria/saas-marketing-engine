"""S3.2: mark QA checklist items pass/fail with comments, and the go-live block.

Deterministic API tests over the real app + a real SQLite session (no mocks). Seeds
`qa_checklist_item` rows directly (S3.1's generation is covered in test_qa_checklist.py) and
exercises the PATCH (pass/fail + comment) and the `POST /go-live` gate that crosses `qa → live`
only when every *blocking* item passes.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session
from app.main import create_app
from app.models import (
    LifecycleState,
    Product,
    QaChecklistItem,
    QaItemStatus,
)


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
def client(session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _product(session, *, state=LifecycleState.QA, slug="auto-author") -> Product:
    product = Product(name="Auto Author", slug=slug, lifecycle_state=state)
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def _seed_items(session, product_id, specs) -> list[int]:
    """specs: list of (blocking, status). Returns the inserted item ids in order."""
    ids = []
    for ord_, (blocking, status) in enumerate(specs, start=1):
        item = QaChecklistItem(
            product_id=product_id,
            ord=ord_,
            instruction=f"step {ord_}",
            blocking=blocking,
            status=status,
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        ids.append(item.id)
    return ids


# --- PATCH: mark pass/fail with comment --------------------------------------------------------


def test_mark_item_pass_with_comment(client, session):
    product = _product(session)
    [item_id] = _seed_items(session, product.id, [(True, QaItemStatus.PENDING)])

    resp = client.patch(
        f"/api/private/qa/{product.id}/checklist/{item_id}",
        json={"status": "pass", "comment": "looks good"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pass"
    assert body["comment"] == "looks good"

    session.expire_all()
    row = session.get(QaChecklistItem, item_id)
    assert row.status == QaItemStatus.PASS
    assert row.comment == "looks good"


def test_mark_item_fail_without_comment(client, session):
    product = _product(session)
    [item_id] = _seed_items(session, product.id, [(True, QaItemStatus.PENDING)])

    resp = client.patch(
        f"/api/private/qa/{product.id}/checklist/{item_id}", json={"status": "fail"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "fail"


def test_patch_404_when_item_not_for_product(client, session):
    product = _product(session)
    other = _product(session, slug="other")
    [item_id] = _seed_items(session, other.id, [(True, QaItemStatus.PENDING)])

    resp = client.patch(
        f"/api/private/qa/{product.id}/checklist/{item_id}", json={"status": "pass"}
    )
    assert resp.status_code == 404


def test_patch_409_when_not_in_qa(client, session):
    product = _product(session, state=LifecycleState.LIVE)
    [item_id] = _seed_items(session, product.id, [(True, QaItemStatus.PASS)])

    resp = client.patch(
        f"/api/private/qa/{product.id}/checklist/{item_id}", json={"status": "fail"}
    )
    assert resp.status_code == 409


# --- go-live gate ------------------------------------------------------------------------------


def test_go_live_full_pass_transitions_to_live(client, session):
    product = _product(session)
    _seed_items(
        session,
        product.id,
        [(True, QaItemStatus.PASS), (True, QaItemStatus.PASS), (False, QaItemStatus.PENDING)],
    )

    resp = client.post(f"/api/private/qa/{product.id}/go-live")
    assert resp.status_code == 200
    assert resp.json()["lifecycle_state"] == "live"

    session.expire_all()
    assert session.get(Product, product.id).lifecycle_state == LifecycleState.LIVE


def test_go_live_blocked_when_blocking_item_pending(client, session):
    product = _product(session)
    _seed_items(session, product.id, [(True, QaItemStatus.PASS), (True, QaItemStatus.PENDING)])

    resp = client.post(f"/api/private/qa/{product.id}/go-live")
    assert resp.status_code == 409

    session.expire_all()
    assert session.get(Product, product.id).lifecycle_state == LifecycleState.QA


def test_go_live_blocked_when_blocking_item_fails(client, session):
    product = _product(session)
    _seed_items(session, product.id, [(True, QaItemStatus.FAIL)])

    resp = client.post(f"/api/private/qa/{product.id}/go-live")
    assert resp.status_code == 409
    session.expire_all()
    assert session.get(Product, product.id).lifecycle_state == LifecycleState.QA


def test_go_live_ignores_non_blocking_failure(client, session):
    product = _product(session)
    _seed_items(session, product.id, [(True, QaItemStatus.PASS), (False, QaItemStatus.FAIL)])

    resp = client.post(f"/api/private/qa/{product.id}/go-live")
    assert resp.status_code == 200
    assert resp.json()["lifecycle_state"] == "live"


def test_go_live_409_without_any_checklist(client, session):
    product = _product(session)  # no items generated yet

    resp = client.post(f"/api/private/qa/{product.id}/go-live")
    assert resp.status_code == 409
    session.expire_all()
    assert session.get(Product, product.id).lifecycle_state == LifecycleState.QA


def test_go_live_409_when_not_in_qa(client, session):
    product = _product(session, state=LifecycleState.SETUP_DONE)
    _seed_items(session, product.id, [(True, QaItemStatus.PASS)])

    resp = client.post(f"/api/private/qa/{product.id}/go-live")
    assert resp.status_code == 409


def test_go_live_404_unknown_product(client, session):
    resp = client.post("/api/private/qa/999/go-live")
    assert resp.status_code == 404
