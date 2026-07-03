"""UTM attribution helpers (TECH_SPEC §6.6, story S6.1).

Three pieces of the attribution chain that both the publisher and the webhook/rollup readers
share:

- `utm_params`/`thread_utm_links`: at publish time, rewrite marketing-domain links in a published
  body so a reader who clicks through carries `utm_content=sme-<content_item.id>` all the way to
  the landing site's funnel capture.
- `parse_utm_content`: the inverse — pull the content item id back out of a captured `utm_content`.
- `resolve_attribution`: the one place that turns a funnel event's UTM fields into
  `(channel_id, content_item_id)`, so the Stripe webhook join (S2.5) and the funnel rollup
  (S6.1 step 3) resolve attribution identically instead of duplicating the join logic.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlmodel import Session, select

from app.models import Channel, ContentItem, Product
from app.models.channel import ChannelType

# `utm_content` values we write always carry this prefix, so parsing back is unambiguous even if a
# landing page or ad network stamps its own `utm_content` convention on other traffic.
_UTM_CONTENT_PREFIX = "sme-"

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_TRAILING_PUNCT = ".,;:!?\"')]}"


def utm_params(product: Product, channel: Channel, item: ContentItem) -> dict[str, str]:
    """The UTM query params a published item's links should carry."""
    return {
        "utm_source": channel.type.value,
        "utm_medium": item.content_type,
        "utm_campaign": product.slug,
        "utm_content": f"{_UTM_CONTENT_PREFIX}{item.id}",
    }


def _marketing_host(marketing_domain: str) -> str:
    """Bare host for a `marketing_domain` that may or may not carry a scheme (matches the
    `allowed_origins`/`_site_base_url` normalization convention elsewhere in the module)."""
    domain = marketing_domain.strip().rstrip("/")
    if "://" in domain:
        return urlsplit(domain).netloc.lower()
    return domain.lower()


def thread_utm_links(body: str, product: Product, channel: Channel, item: ContentItem) -> str:
    """Rewrite every link in `body` pointing at `product.marketing_domain` to carry this item's
    UTM params, merging with any existing query string (ours win on key collision). Bodies with no
    `marketing_domain` or no matching link are returned unchanged."""
    if not product.marketing_domain:
        return body
    host = _marketing_host(product.marketing_domain)
    if not host:
        return body
    params = utm_params(product, channel, item)

    def _rewrite(match: re.Match[str]) -> str:
        raw = match.group(0)
        core = raw.rstrip(_TRAILING_PUNCT)
        suffix = raw[len(core) :]
        parts = urlsplit(core)
        if parts.netloc.lower() != host:
            return raw
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(params)
        new_query = urlencode(query)
        return (
            urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)) + suffix
        )

    return _URL_RE.sub(_rewrite, body)


def parse_utm_content(value: str | None) -> int | None:
    """Inverse of `utm_params`'s `utm_content`: `"sme-<id>"` -> `<id>`, else None."""
    if value is None or not value.startswith(_UTM_CONTENT_PREFIX):
        return None
    tail = value[len(_UTM_CONTENT_PREFIX) :]
    # isascii too: `"²".isdigit()` is True but `int("²")` raises, and utm_content is
    # visitor-controlled — a crafted value must parse to None, not 500 the webhook.
    return int(tail) if tail.isascii() and tail.isdigit() else None


def resolve_attribution(
    session: Session, product_id: int, utm_source: str | None, utm_content: str | None
) -> tuple[int | None, int | None]:
    """Resolve `(channel_id, content_item_id)` for a product from a funnel event's UTM fields.

    Primary: `utm_content` parses to a content item, validated against `product_id` — its
    `channel_id` + id are used. Fallback: `utm_source` matches a `ChannelType` value — that
    product's channel of that type gives `channel_id` only (no content item to point at).
    Unresolvable on both -> `(None, None)`.
    """
    item_id = parse_utm_content(utm_content)
    if item_id is not None:
        content_item = session.get(ContentItem, item_id)
        if content_item is not None and content_item.product_id == product_id:
            return content_item.channel_id, content_item.id

    if utm_source:
        try:
            channel_type = ChannelType(utm_source)
        except ValueError:
            return None, None
        channel = session.exec(
            select(Channel).where(Channel.product_id == product_id, Channel.type == channel_type)
        ).first()
        if channel is not None:
            return channel.id, None

    return None, None
