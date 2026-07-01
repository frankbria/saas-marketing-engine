"""Content API (private dashboard, story S4.7 — retract).

Surfaces published/retracted items so the operator can pull a bad live post. `POST .../retract`
calls the §7 channel adapter's `delete(external_url)`, flips the item to `retracted`, and removes
the remote post where the API allows. Retract is only valid on a `published` item (409 otherwise);
a transient adapter failure surfaces as 503 so the operator retries rather than lose the live post.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, col, select

from app.channels.base import Retryable
from app.db import get_session
from app.models import ContentItem, Product
from app.models.content_item import ContentItemStatus
from app.modules.crank.retract import retract_item

router = APIRouter(prefix="/content", tags=["content"])

SessionDep = Annotated[Session, Depends(get_session)]


def _require_product(session: Session, product_id: int) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    return product


@router.get("/{product_id}")
def list_content(product_id: int, session: SessionDep) -> list[ContentItem]:
    """Published + retracted items for the dashboard retract list (newest first)."""
    _require_product(session, product_id)
    return session.exec(
        select(ContentItem)
        .where(
            ContentItem.product_id == product_id,
            col(ContentItem.status).in_([ContentItemStatus.PUBLISHED, ContentItemStatus.RETRACTED]),
        )
        .order_by(col(ContentItem.published_at).desc(), col(ContentItem.id).desc())
    ).all()


@router.post("/{product_id}/{item_id}/retract")
def retract_content(product_id: int, item_id: int, session: SessionDep) -> ContentItem:
    """Retract a published item: delete the remote post + mark `retracted`."""
    _require_product(session, product_id)
    item = session.get(ContentItem, item_id)
    if item is None or item.product_id != product_id:
        raise HTTPException(status_code=404, detail="content item not found for this product")
    if item.status != ContentItemStatus.PUBLISHED:
        raise HTTPException(
            status_code=409, detail=f"item is {item.status}, only a published item can be retracted"
        )
    if not item.external_url:
        # Broken invariant (published without a remote handle): don't mark it retracted while the
        # live post is unreachable — surface it instead.
        raise HTTPException(status_code=409, detail="published item has no external_url to retract")
    try:
        return retract_item(session, item)
    except Retryable as exc:
        # Transient adapter failure — the post is still live. Surface so the operator retries.
        raise HTTPException(status_code=503, detail=f"retract failed transiently: {exc}") from exc
