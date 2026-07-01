"""Retract a published item (TECH_SPEC §7, story S4.7).

The kill switch (S4.6) only stops *future* posts; retract pulls a *live* one. Operator-initiated
(synchronous, from the dashboard), so — unlike `publish_scheduled` — it does not swallow transient
failures: it calls the §7 channel adapter's `delete(external_url)`, flips the item to `retracted`,
and lets any adapter error propagate to the API layer. The item stays `published` on failure so the
operator can retry rather than losing track of a post that's still live.
"""

from __future__ import annotations

from sqlmodel import Session

from app.channels.base import get_adapter
from app.models import Channel, ContentItem, Product
from app.models.content_item import ContentItemStatus
from app.secrets.vault import get_credential


def retract_item(session: Session, item: ContentItem, *, adapter_for=get_adapter) -> ContentItem:
    """Delete a published item's remote post and mark it `retracted`.

    Caller guarantees `item.status == PUBLISHED`. `adapter_for` is injectable so tests drive the
    full path with a stub adapter (no network), mirroring `publish_scheduled`. Adapter errors
    (`Retryable` or permanent) propagate — the item is left `published` for a retry.
    """
    # external_url is the handle the adapter deletes by. A published item always has one; a NULL is
    # a broken invariant — fail closed rather than "delete" an empty handle (a blog no-op that would
    # leave the live post up while we mark it retracted).
    if not item.external_url:
        raise ValueError(f"content_item {item.id} is published but has no external_url to retract")
    channel = session.get(Channel, item.channel_id)
    product = session.get(Product, item.product_id)
    if channel is None or product is None:
        raise LookupError(f"content_item {item.id} has no channel/product to retract from")

    adapter = adapter_for(channel.type)
    creds = (
        get_credential(session, product.id, adapter.credential_key, channel_id=channel.id)
        if adapter.credential_key
        else None
    )
    adapter.delete(item.external_url, product, channel, creds)

    item.status = ContentItemStatus.RETRACTED
    item.error = None
    session.add(item)
    session.commit()
    session.refresh(item)
    return item
