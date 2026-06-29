"""Channels API (private dashboard, story S2.6).

`POST /{product_id}/setup` enqueues the `setup_channels` job (the worker generates per-channel
profiles + the human setup checklist). The GETs expose channels + checklist to the dashboard.
`POST /{product_id}/{channel_id}/connect` is the OAuth connect flow's server half: the dashboard
runs each platform's own OAuth dance and posts the resulting token here, which the engine encrypts
into the vault (channel-scoped) and marks `connect_state=connected`. `PATCH .../checklist/{item_id}`
toggles a human step done/pending.

ponytail: per-provider authorize→callback redirect is deferred to the operational/refresh story
(S4.8); this endpoint is the vault-write + connect_state half S2.6 actually requires, and it stays
testable against the real vault (no provider mock, per the no-mocking house rule).
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.models import (
    Channel,
    ConnectState,
    LifecycleState,
    Product,
    SetupChecklistItem,
    SetupItemStatus,
)
from app.secrets import vault
from app.worker import enqueue

router = APIRouter(prefix="/channels", tags=["channels"])

SessionDep = Annotated[Session, Depends(get_session)]


class ConnectRequest(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    account_ref: str | None = None  # the connected handle/username, if known


class ChecklistUpdate(BaseModel):
    status: SetupItemStatus


def _require_product(session: Session, product_id: int) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    return product


@router.post("/{product_id}/setup", status_code=202)
def trigger_channel_setup(product_id: int, session: SessionDep) -> dict:
    """Enqueue channel-account prep (profiles + human checklist). Gated like other setup jobs."""
    product = _require_product(session, product_id)
    if product.lifecycle_state not in (
        LifecycleState.SETUP_READY,
        LifecycleState.SETUP_DONE,
    ):
        raise HTTPException(
            status_code=409,
            detail=f"product is {product.lifecycle_state}, not setup_ready; approve strategy first",
        )
    if not product.brand_json:
        raise HTTPException(status_code=400, detail="brand kit not generated yet")
    job = enqueue(session, "setup_channels", product_id=product_id)
    return {"job_id": job.id, "status": job.status}


@router.get("/{product_id}")
def list_channels(product_id: int, session: SessionDep) -> list[Channel]:
    _require_product(session, product_id)
    return session.exec(
        select(Channel).where(Channel.product_id == product_id).order_by(Channel.id)
    ).all()


@router.get("/{product_id}/checklist")
def list_checklist(product_id: int, session: SessionDep) -> list[SetupChecklistItem]:
    _require_product(session, product_id)
    return session.exec(
        select(SetupChecklistItem)
        .where(SetupChecklistItem.product_id == product_id)
        .order_by(SetupChecklistItem.ord)
    ).all()


@router.post("/{product_id}/{channel_id}/connect")
def connect_channel(
    product_id: int, channel_id: int, payload: ConnectRequest, session: SessionDep
) -> Channel:
    """Store an OAuth token in the vault and flip the channel to `connected`."""
    _require_product(session, product_id)
    channel = session.get(Channel, channel_id)
    if channel is None or channel.product_id != product_id:
        raise HTTPException(status_code=404, detail="channel not found for this product")
    if not payload.access_token:
        raise HTTPException(status_code=400, detail="access_token is required")

    vault.put_credential(
        session,
        product_id,
        f"{channel.type.value}_oauth",
        payload.access_token,
        channel_id=channel_id,
        expires_at=payload.expires_at,
    )
    if payload.refresh_token:
        vault.put_credential(
            session,
            product_id,
            f"{channel.type.value}_oauth_refresh",
            payload.refresh_token,
            channel_id=channel_id,
        )

    channel.connect_state = ConnectState.CONNECTED
    if payload.account_ref:
        channel.account_ref = payload.account_ref
    channel.updated_at = datetime.now(UTC)
    session.add(channel)
    session.commit()
    session.refresh(channel)
    return channel


@router.patch("/{product_id}/checklist/{item_id}")
def update_checklist_item(
    product_id: int, item_id: int, payload: ChecklistUpdate, session: SessionDep
) -> SetupChecklistItem:
    _require_product(session, product_id)
    item = session.get(SetupChecklistItem, item_id)
    if item is None or item.product_id != product_id:
        raise HTTPException(status_code=404, detail="checklist item not found for this product")
    item.status = payload.status
    item.updated_at = datetime.now(UTC)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item
