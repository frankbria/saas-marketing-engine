"""Channels API (private dashboard, story S2.6).

`POST /{product_id}/setup` enqueues the `setup_channels` job (the worker generates per-channel
profiles + the human setup checklist). The GETs expose channels + checklist to the dashboard.
`POST /{product_id}/{channel_id}/connect` is the OAuth connect flow's server half: the dashboard
runs each platform's own OAuth dance and posts the resulting credential here, which the engine
encrypts into the vault (channel-scoped) and marks `connect_state=connected`. The credential *shape*
is per-provider (S4.8.1): a self-managed provider (Reddit via PRAW) posts the structured `reddit`
block that the adapter consumes as-is; an owned provider posts a bare `access_token` we hold and
refresh ourselves (S4.8). `PATCH .../checklist/{item_id}` toggles a human step done/pending.

S4.8.2 adds the full redirect flow for owned-token providers: `POST .../credentials` seeds the
OAuth-app client id/secret, `GET .../authorize` redirects to the provider consent screen, and
`GET .../callback` exchanges the code for tokens (all via `modules/crank/oauth_refresh`), reusing
the same vault-write + `connect_state=connected` half and auto-completing the oauth checklist step.
The manual `/connect` paste path stays as a fallback. Everything stays testable against the real
vault (no provider mock — the network exchange is injected at the module seam, per the house rule).
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, field_validator
from sqlmodel import Session, select

from app.config import settings
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
from app.modules.crank.oauth_refresh import (
    OWNED_TOKEN_PROVIDERS,
    InvalidState,
    build_authorize_url,
    exchange_authorization_code,
    mint_state,
    verify_state,
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


class SeedCredentialsRequest(BaseModel):
    """OAuth-app client credentials for an owned-token provider (S4.8.2). Seeded through the flow so
    they never have to be hand-loaded; stored channel-scoped, encrypted, auto-redacted."""

    client_id: str
    client_secret: str

    @field_validator("*")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()


def _require_product(session: Session, product_id: int) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    return product


def _require_channel(session: Session, product_id: int, channel_id: int) -> Channel:
    _require_product(session, product_id)
    channel = session.get(Channel, channel_id)
    if channel is None or channel.product_id != product_id:
        raise HTTPException(status_code=404, detail="channel not found for this product")
    return channel


def _complete_oauth_checklist(session: Session, product_id: int, channel_id: int) -> None:
    """Mark this channel's `oauth` setup step done — a successful connect auto-completes it, so the
    operator doesn't tick it by hand. No-op if the item is absent (e.g. setup not yet run)."""
    item = session.exec(
        select(SetupChecklistItem).where(
            SetupChecklistItem.product_id == product_id,
            SetupChecklistItem.channel_id == channel_id,
            SetupChecklistItem.category == "oauth",
        )
    ).first()
    if item is not None and item.status != SetupItemStatus.DONE:
        item.status = SetupItemStatus.DONE
        item.updated_at = datetime.now(UTC)
        session.add(item)


def _mark_connected(
    session: Session, product_id: int, channel: Channel, *, account_ref: str | None = None
) -> Channel:
    """Flip a channel to `connected`, record its handle if known, and auto-complete the oauth
    checklist step. Callers persist the credential(s) to the vault first. Commits + refreshes."""
    assert (
        channel.id is not None
    )  # persisted channel (from _require_channel) — for the type checker
    channel.connect_state = ConnectState.CONNECTED
    if account_ref:
        channel.account_ref = account_ref
    channel.updated_at = datetime.now(UTC)
    session.add(channel)
    _complete_oauth_checklist(session, product_id, channel.id)
    session.commit()
    session.refresh(channel)
    return channel


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
    channel = _require_channel(session, product_id, channel_id)

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

    return _mark_connected(session, product_id, channel, account_ref=payload.account_ref)


def _require_owned_provider(channel: Channel):
    """The channel's registered owned-token provider, or 400 if its type has none (blog has no
    OAuth; Reddit self-manages via PRAW; X/IG/YT aren't registered — those stay manual)."""
    provider = OWNED_TOKEN_PROVIDERS.get(channel.type)
    if provider is None:
        raise HTTPException(
            status_code=400,
            detail=f"{channel.type.value} has no redirect-based OAuth provider registered",
        )
    return provider


def _callback_redirect_uri(product_id: int, channel_id: int) -> str:
    """The `redirect_uri` sent at authorize and re-sent at token exchange — must be byte-identical
    for both legs (providers compare it), so it is built here once."""
    base = settings.oauth_redirect_base_url.rstrip("/")
    return f"{base}/api/private/channels/{product_id}/{channel_id}/callback"


@router.post("/{product_id}/{channel_id}/credentials", status_code=204)
def seed_client_credentials(
    product_id: int, channel_id: int, payload: SeedCredentialsRequest, session: SessionDep
) -> None:
    """Store an owned-token provider's OAuth-app `client_id`/`client_secret` (channel-scoped,
    encrypted) so the authorize/refresh legs can read them — no hand-loading into the vault."""
    channel = _require_channel(session, product_id, channel_id)
    _require_owned_provider(channel)
    prefix = channel.type.value
    vault.put_credential(
        session, product_id, f"{prefix}_client_id", payload.client_id, channel_id=channel_id
    )
    vault.put_credential(
        session, product_id, f"{prefix}_client_secret", payload.client_secret, channel_id=channel_id
    )


def _require_client_credentials(
    session: Session, product_id: int, channel_id: int, prefix: str
) -> tuple[str, str]:
    """Both seeded OAuth-app credentials, or 400. Checked *before* the authorize redirect too (not
    just at callback) so a half-seeded channel fails early — never after the operator has spent the
    one-time consent code."""
    client_id = vault.get_credential(
        session, product_id, f"{prefix}_client_id", channel_id=channel_id
    )
    client_secret = vault.get_credential(
        session, product_id, f"{prefix}_client_secret", channel_id=channel_id
    )
    if not (client_id and client_secret):
        raise HTTPException(
            status_code=400, detail=f"seed {prefix} client credentials before connecting"
        )
    return client_id, client_secret


@router.get("/{product_id}/{channel_id}/authorize")
def authorize_channel(product_id: int, channel_id: int, session: SessionDep) -> RedirectResponse:
    """Begin the OAuth dance: redirect the operator's browser to the provider's consent screen with
    the seeded client id, requested scopes, our callback `redirect_uri`, and a signed `state`."""
    channel = _require_channel(session, product_id, channel_id)
    provider = _require_owned_provider(channel)
    client_id, _ = _require_client_credentials(session, product_id, channel_id, channel.type.value)
    url = build_authorize_url(
        provider,
        client_id,
        _callback_redirect_uri(product_id, channel_id),
        mint_state(product_id, channel_id),
    )
    return RedirectResponse(url, status_code=302)


@router.get("/{product_id}/{channel_id}/callback")
def oauth_callback(
    product_id: int, channel_id: int, code: str, state: str, session: SessionDep
) -> RedirectResponse:
    """Complete the OAuth dance: validate `state`, exchange `code` for tokens using the seeded
    client credentials, persist them channel-scoped, flip the channel to `connected`, and bounce
    the operator's browser to the dashboard. Query params never logged; tokens auto-redacted."""
    channel = _require_channel(session, product_id, channel_id)
    provider = _require_owned_provider(channel)
    try:
        verify_state(state, product_id, channel_id)
    except InvalidState as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    prefix = channel.type.value
    client_id, client_secret = _require_client_credentials(session, product_id, channel_id, prefix)

    try:
        access_token, refresh_token, expires_at = exchange_authorization_code(
            provider,
            code,
            _callback_redirect_uri(product_id, channel_id),
            client_id,
            client_secret,
            datetime.now(UTC),
        )
    except Exception as exc:
        # Provider/network/parse failure — translate at the boundary (never leak provider detail)
        # so the channel stays `pending` and the operator gets a clean retry, not an opaque 500.
        raise HTTPException(status_code=502, detail="OAuth token exchange failed") from exc

    # Stage both tokens and the connect/checklist flip in one transaction (commit=False) so a
    # failure mid-write can't leave a half-connected channel — `_mark_connected` commits atomically.
    vault.put_credential(
        session,
        product_id,
        f"{prefix}_oauth",
        access_token,
        channel_id=channel_id,
        expires_at=expires_at,
        commit=False,
    )
    if refresh_token:
        vault.put_credential(
            session,
            product_id,
            f"{prefix}_oauth_refresh",
            refresh_token,
            channel_id=channel_id,
            commit=False,
        )
    _mark_connected(session, product_id, channel)

    dashboard = settings.dashboard_base_url.rstrip("/")
    return RedirectResponse(f"{dashboard}/products/{product_id}", status_code=302)


@router.patch("/{product_id}/{channel_id}/pause")
def set_channel_paused(
    product_id: int, channel_id: int, payload: PauseRequest, session: SessionDep
) -> Channel:
    """Flip the per-channel kill switch (S4.6). `publish_scheduled` re-checks `paused` immediately
    before every publish, so pausing halts new posts within one tick and resuming restores the
    schedule (items stay `scheduled`, nothing is lost)."""
    channel = _require_channel(session, product_id, channel_id)
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
