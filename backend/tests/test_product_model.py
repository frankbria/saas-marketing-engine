"""S0.3: Product model defaults, enums, and roundtrip."""

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine

from app.models.product import LifecycleState, MonetizationModel, Product


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


def test_defaults(session):
    p = Product(name="Auto Author", slug="auto-author")
    session.add(p)
    session.commit()
    session.refresh(p)

    assert p.id is not None
    assert p.monetization_model == MonetizationModel.CC_SUB
    assert p.lifecycle_state == LifecycleState.DRAFT
    assert p.token_budget_cents_month == 0
    assert p.created_at is not None and p.updated_at is not None
    # folded fields default empty until S1/S2 populate them
    assert p.brand_json is None
    assert p.stripe_price_id is None


def test_slug_unique(session):
    session.add(Product(name="A", slug="dup"))
    session.commit()
    session.add(Product(name="B", slug="dup"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_roundtrip_all_fields(session):
    p = Product(
        name="Widget",
        slug="widget",
        repo_url="https://github.com/frankbria/widget",
        description="a widget",
        monetization_model=MonetizationModel.TRIAL,
        marketing_domain="widget.app",
        token_budget_cents_month=5000,
    )
    session.add(p)
    session.commit()
    session.refresh(p)

    assert p.monetization_model == MonetizationModel.TRIAL
    assert p.marketing_domain == "widget.app"
    assert p.token_budget_cents_month == 5000
