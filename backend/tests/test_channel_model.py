"""S2.6: channel + setup_checklist_item models persist and enforce their invariants."""

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine

from app.models import (
    AUTONOMOUS_TYPES,
    Channel,
    ChannelType,
    ConnectState,
    SetupChecklistItem,
    SetupItemStatus,
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


def test_channel_defaults_and_persist(session):
    chan = Channel(product_id=1, type=ChannelType.REDDIT)
    session.add(chan)
    session.commit()
    session.refresh(chan)

    assert chan.id is not None
    assert chan.connect_state == ConnectState.PENDING
    assert chan.enabled is True
    assert chan.paused is False
    assert chan.profile_json is None


def test_channel_type_unique_per_product(session):
    session.add(Channel(product_id=1, type=ChannelType.BLOG))
    session.commit()
    session.add(Channel(product_id=1, type=ChannelType.BLOG))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    # same type, different product is fine
    session.add(Channel(product_id=2, type=ChannelType.BLOG))
    session.commit()


def test_autonomous_types_are_blog_and_reddit():
    assert AUTONOMOUS_TYPES == frozenset({ChannelType.BLOG, ChannelType.REDDIT})


def test_setup_checklist_item_defaults(session):
    item = SetupChecklistItem(
        product_id=1, channel_id=None, ord=0, instruction="Set up DNS", category="dns"
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    assert item.status == SetupItemStatus.PENDING
    assert item.channel_id is None
