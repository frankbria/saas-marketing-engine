"""Owned-blog publish adapter (TECH_SPEC §7, story S4.5).

The owned site carries zero ToS risk, so this is a direct file write into the product's workspace
site tree (`site/blog/<post-slug>.html`) — the same tree S2.1 renders and deploys under the
product's `marketing_domain`. Serving/redeploy of the new file is operational (like site.py's deploy
note). Pure filesystem, no network: idempotent by construction (writing the same path overwrites),
so the file's existence *is* the remote check.
"""

from __future__ import annotations

import html
import json
import os
import re

from app.channels.base import PublishResult
from app.config import settings
from app.models import Channel, ContentItem, Product
from app.models.channel import ChannelType
from app.workspace import workspace_path

# The post slug becomes a filename — sanitize hard. It can originate from LLM `meta_json.slug`, so
# strip it to a safe slug (lowercase alnum + dashes) to close path traversal; empty ⇒ item-id.
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _post_slug(item: ContentItem) -> str:
    # The LLM-provided slug is only a human-readable prefix — always suffix the item id so two items
    # that happen to share a slug get distinct files/URLs (no cross-item overwrite), while
    # re-publishing the *same* item stays on the same path (idempotent on item id).
    raw = ""
    if item.meta_json:
        raw = (json.loads(item.meta_json) or {}).get("slug") or ""
    base = _SLUG_STRIP.sub("-", raw.lower()).strip("-") or "post"
    return f"{base}-{item.id}"


def _external_url(product: Product, post_slug: str) -> str:
    base = (
        f"https://{product.marketing_domain}"
        if product.marketing_domain
        else settings.public_api_base_url.rstrip("/")
    )
    return f"{base}/blog/{post_slug}"


class BlogAdapter:
    type = ChannelType.BLOG
    credential_key = None  # owned site: no external credential

    def publish(
        self, item: ContentItem, product: Product, channel: Channel, creds: str | None
    ) -> PublishResult:
        post_slug = _post_slug(item)
        blog_dir = workspace_path(product.slug) / "site" / "blog"
        blog_dir.mkdir(parents=True, exist_ok=True)
        title = item.title or ""
        page = (
            f'<!doctype html>\n<html><head><meta charset="utf-8">'
            f"<title>{html.escape(title)}</title></head>\n"
            f"<body>\n<article>\n<h1>{html.escape(title)}</h1>\n"
            f"<div>{html.escape(item.body)}</div>\n</article>\n</body></html>\n"
        )
        # Write atomically (temp file + os.replace) so a crash mid-write can't corrupt an already
        # published page; re-publishing the same item lands the same path (overwrite-safe).
        target = blog_dir / f"{post_slug}.html"
        tmp = blog_dir / f".{post_slug}.html.tmp"
        tmp.write_text(page, encoding="utf-8")
        os.replace(tmp, target)
        return PublishResult(external_url=_external_url(product, post_slug))

    def delete(
        self, external_url: str, product: Product, channel: Channel, creds: str | None
    ) -> None:
        # Derive the file from the URL's last path segment; no-op if already gone (retract, S4.7).
        post_slug = external_url.rstrip("/").rsplit("/", 1)[-1]
        path = workspace_path(product.slug) / "site" / "blog" / f"{post_slug}.html"
        path.unlink(missing_ok=True)
