"""Owned-podcast publish adapter — RSS feed on the product's static site (TECH_SPEC §7, story S5.2).

The owned RSS feed carries zero ToS risk (like the owned blog), so this is a direct file write into
the product's workspace site tree — the same tree S2.1 renders and `site.py` deploys under the
product's `marketing_domain`. Publishing an episode:

1. copies the episode MP3 (`item.media_ref`) into `site/podcast/<slug>.mp3`,
2. writes a per-episode sidecar (`<slug>.json`) with the metadata the feed needs,
3. writes a minimal episode page (`<slug>.html`) — the human-facing `external_url`,
4. (re)builds `site/podcast/feed.xml` (RSS 2.0 + iTunes tags) from *all* sidecars in the directory.

The feed is rebuilt from the filesystem, not the DB, so the adapter needs no session (matching the
`ChannelAdapter` contract and the blog adapter's filesystem-is-truth ethos). Idempotent by
construction: re-publishing the same item lands the same slug (title + item id), overwrites the same
files, and produces the same single feed entry (guid keys off the item id).
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
from email.utils import format_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from app.channels.base import PublishResult
from app.config import settings
from app.models import Channel, ContentItem, Product
from app.models.channel import ChannelType
from app.workspace import workspace_path

_ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"

# The episode slug becomes a filename — sanitize hard. The prefix can originate from the LLM title,
# so strip it to a safe slug (lowercase alnum + dashes) to close path traversal; empty ⇒ item-id.
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _episode_slug(item: ContentItem) -> str:
    # The title is only a human-readable prefix — always suffix the item id so two episodes that
    # share a title get distinct files/URLs (no cross-item overwrite), while re-publishing the
    # *same* item stays on the same path (idempotent on item id).
    base = _SLUG_STRIP.sub("-", (item.title or "").lower()).strip("-") or "episode"
    return f"{base}-{item.id}"


def _site_base(product: Product) -> str:
    return (
        f"https://{product.marketing_domain}"
        if product.marketing_domain
        else settings.public_api_base_url.rstrip("/")
    )


def _podcast_dir(product: Product) -> Path:
    return workspace_path(product.slug) / "site" / "podcast"


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class PodcastAdapter:
    type = ChannelType.PODCAST
    credential_key = None  # owned RSS feed: no external credential

    def publish(
        self, item: ContentItem, product: Product, channel: Channel, creds: str | None
    ) -> PublishResult:
        # Fail closed: a missing media_ref is a broken upstream contract (the render/finalize step
        # sets it), not a transient error — publishing an episode with no audio would ship an empty
        # enclosure that looks fine until someone hits play.
        if not item.media_ref:
            raise RuntimeError(
                f"content_item {item.id} has no media_ref; nothing to publish to the podcast feed"
            )
        source = Path(settings.workspace_root) / item.media_ref
        if not source.is_file():
            raise RuntimeError(
                f"content_item {item.id} media_ref {item.media_ref!r} does not resolve to a file"
            )

        podcast_dir = _podcast_dir(product)
        podcast_dir.mkdir(parents=True, exist_ok=True)
        slug = _episode_slug(item)

        # Copy the audio in atomically (temp + replace) so a crash mid-copy can't leave a truncated
        # enclosure the feed already points at.
        audio_name = f"{slug}.mp3"
        tmp_audio = podcast_dir / f".{audio_name}.tmp"
        shutil.copyfile(source, tmp_audio)
        os.replace(tmp_audio, podcast_dir / audio_name)

        meta = json.loads(item.meta_json) if item.meta_json else {}
        description = meta.get("description") or item.body
        # RSS pubDate = the release time. The publish pass sets `published_at` only *after* this
        # adapter returns, so it is still None here; `scheduled_for` (set by pacing) is the intended
        # release time and is stable across idempotent re-publishes — prefer it so a scheduled or
        # backlogged episode advertises its release date and the feed sorts by release, not
        # generation. Fall back to published_at (a manual/unpaced publish) then created_at.
        pub_dt = item.scheduled_for or item.published_at or item.created_at
        sidecar = {
            "slug": slug,
            "title": item.title or slug,
            "description": description,
            "guid": f"sme-podcast-{item.id}",
            "pubdate": pub_dt.isoformat(),
            "audio": audio_name,
            "length": os.path.getsize(podcast_dir / audio_name),
        }
        _atomic_write_text(podcast_dir / f"{slug}.json", json.dumps(sidecar))

        base = _site_base(product)
        episode_url = f"{base}/podcast/{slug}.html"
        _atomic_write_text(podcast_dir / f"{slug}.html", _episode_page(sidecar, base, product))
        _rebuild_feed(podcast_dir, product, base)
        return PublishResult(external_url=episode_url)

    def delete(
        self, external_url: str, product: Product, channel: Channel, creds: str | None
    ) -> None:
        # Derive the slug from the URL's last path segment (strip the .html); prune the episode's
        # files and rebuild the feed without it. No-op on already-gone files (retract, S4.7).
        slug = external_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".html")
        podcast_dir = _podcast_dir(product)
        for name in (f"{slug}.mp3", f"{slug}.json", f"{slug}.html"):
            (podcast_dir / name).unlink(missing_ok=True)
        _rebuild_feed(podcast_dir, product, _site_base(product))


def _episode_page(sidecar: dict, base: str, product: Product) -> str:
    title = html.escape(sidecar["title"])
    audio_url = f"{base}/podcast/{sidecar['audio']}"
    return (
        f'<!doctype html>\n<html><head><meta charset="utf-8">'
        f"<title>{title}</title>"
        f'<link rel="alternate" type="application/rss+xml" '
        f'title="{html.escape(product.name)} podcast" href="{base}/podcast/feed.xml">'
        f"</head>\n<body>\n<article>\n<h1>{title}</h1>\n"
        f'<audio controls src="{html.escape(audio_url)}"></audio>\n'
        f"<div>{html.escape(sidecar['description'])}</div>\n"
        f"</article>\n</body></html>\n"
    )


def _rebuild_feed(podcast_dir: Path, product: Product, base: str) -> None:
    """Regenerate feed.xml from every episode sidecar in the directory (newest first). ElementTree
    guarantees well-formed, correctly-escaped XML; writing the same set of sidecars is deterministic
    so the rebuild is idempotent."""
    episodes = []
    for sidecar_path in podcast_dir.glob("*.json"):
        try:
            episodes.append(json.loads(sidecar_path.read_text()))
        except json.JSONDecodeError:
            continue  # a corrupt sidecar must not break the whole feed
    episodes.sort(key=lambda e: e.get("pubdate", ""), reverse=True)

    # register_namespace makes ElementTree emit `xmlns:itunes="…"` on the root automatically from
    # the `{ns}tag` qualified names below — setting it manually too would duplicate the attribute.
    ET.register_namespace("itunes", _ITUNES_NS)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"{product.name} Podcast"
    ET.SubElement(channel, "link").text = f"{base}/podcast/feed.xml"
    ET.SubElement(channel, "description").text = (
        product.marketing_domain or product.name
    ) + " — automated marketing podcast"
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, f"{{{_ITUNES_NS}}}author").text = product.name

    for ep in episodes:
        audio_url = f"{base}/podcast/{ep['audio']}"
        entry = ET.SubElement(channel, "item")
        ET.SubElement(entry, "title").text = ep["title"]
        ET.SubElement(entry, "description").text = ep["description"]
        ET.SubElement(entry, "link").text = f"{base}/podcast/{ep['slug']}.html"
        guid = ET.SubElement(entry, "guid", {"isPermaLink": "false"})
        guid.text = ep["guid"]
        pub = ep.get("pubdate")
        if pub:
            ET.SubElement(entry, "pubDate").text = _rfc822(pub)
        ET.SubElement(
            entry,
            "enclosure",
            {"url": audio_url, "length": str(ep.get("length", 0)), "type": "audio/mpeg"},
        )

    xml = ET.tostring(rss, encoding="unicode", xml_declaration=True)
    _atomic_write_text(podcast_dir / "feed.xml", xml + "\n")


def _rfc822(iso: str) -> str:
    """RFC-822 date for RSS pubDate. Falls back to the raw ISO string if it can't be parsed rather
    than dropping the episode from the feed."""
    from datetime import UTC, datetime

    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    # SQLite round-trips datetimes as naive; the app stores UTC (datetime.now(UTC)), so treat a
    # naive value as UTC and emit an explicit `GMT` date — a bare offset-less RFC-822 date parses as
    # "-0000" (unknown offset), which is technically undated for strict RSS readers.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return format_datetime(dt, usegmt=True)
