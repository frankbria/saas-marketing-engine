"""Channels API (private dashboard, story S2.6).

`POST /{product_id}/setup` enqueues the `setup_channels` job (the worker generates per-channel
profiles + the human setup checklist). The GETs expose channels + checklist to the dashboard.
`POST /{product_id}/{channel_id}/connect` is the OAuth connect flow's server half: the dashboard
runs each platform's own OAuth dance and posts the resulting credential here, which the engine
encrypts into the vault (channel-scoped) and marks `connect_state=connected`. The credential *shape*
is per-provider (S4.8.1): a self-managed provider (Reddit via PRAW) posts the structured `reddit`
block that the adapter consumes as-is; an owned provider posts a bare `access_token` we hold and
refresh ourselves (S4.8). `PATCH .../checklist/{item_id}` toggles a human step done/pending.

ponytail: per-provider authorize→callback redirect is deferred to S4.8.2 (#65); this endpoint is the
vault-write + connect_state half, and it stays testable against the real vault (no provider mock,
per the no-mocking house rule).
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlmodel import Session, select

from app.db import get_session
from app.models import (
    SELF_MANAGED_TYPES,
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


class RedditCredential(BaseModel):
    """The documented Reddit credential shape (S4.8.1) — PRAW script-app kwargs. Stored verbatim as
    JSON under `reddit_oauth`; `RedditAdapter._parse_creds` builds its PRAW client from exactly
    these fields, and `oauth_refresh.is_self_managed_credential` sees a JSON object and skips
    proactive refresh (PRAW self-refreshes the access token from `refresh_token`)."""

    client_id: str
    client_secret: str
    refresh_token: str
    user_agent: str

    @field_validator("*")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        # Every PRAW field is required to build a working client; a blank one would store a
        # credential that marks the channel connected but fails at publish. Reject + trim.
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()


class ConnectRequest(BaseModel):
    # Owned providers (a bare access token we hold + refresh ourselves, S4.8):
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: datetime | None = None
    # Self-managed providers (Reddit/PRAW): the structured credential the adapter consumes directly.
    reddit: RedditCredential | None = None
    account_ref: str | None = None  # the connected handle/username, if known

    @field_validator("access_token", "refresh_token", "account_ref", mode="before")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        # A whitespace-only token would pass the truthiness guard and store a connected-but-broken
        # credential; collapse blanks to None so the missing-token path (400) fires instead.
        if isinstance(v, str):
            return v.strip() or None
        return v


class ChecklistUpdate(BaseModel):
    status: SetupItemStatus


class PauseRequest(BaseModel):
    paused: bool


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
    """Store a channel's OAuth credential in the vault and flip it to `connected`.

    Credential shape is per-provider (S4.8.1): a self-managed provider stores its structured blob
    verbatim under `{type}_oauth` (no separate refresh cred, no expiry — its client self-refreshes);
    an owned provider stores the bare `access_token` (+ optional refresh token / expiry)."""
    _require_product(session, product_id)
    channel = session.get(Channel, channel_id)
    if channel is None or channel.product_id != product_id:
        raise HTTPException(status_code=404, detail="channel not found for this product")

    key = f"{channel.type.value}_oauth"
    if channel.type in SELF_MANAGED_TYPES:
        if payload.reddit is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{channel.type.value} is self-managed; a `reddit` credential block "
                    "(client_id, client_secret, refresh_token, user_agent) is required"
                ),
            )
        vault.put_credential(
            session, product_id, key, payload.reddit.model_dump_json(), channel_id=channel_id
        )
    else:
        if not payload.access_token:
            raise HTTPException(status_code=400, detail="access_token is required")
        vault.put_credential(
            session,
            product_id,
            key,
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


@router.patch("/{product_id}/{channel_id}/pause")
def set_channel_paused(
    product_id: int, channel_id: int, payload: PauseRequest, session: SessionDep
) -> Channel:
    """Flip the per-channel kill switch (S4.6). `publish_scheduled` re-checks `paused` immediately
    before every publish, so pausing halts new posts within one tick and resuming restores the
    schedule (items stay `scheduled`, nothing is lost)."""
    _require_product(session, product_id)
    channel = session.get(Channel, channel_id)
    if channel is None or channel.product_id != product_id:
        raise HTTPException(status_code=404, detail="channel not found for this product")
    channel.paused = payload.paused
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
