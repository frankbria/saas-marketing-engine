"""S5.2: owned-podcast RSS publish adapter (issue #30).

The adapter copies a rendered episode MP3 into the product's static site tree, writes an episode
page, and (re)builds an RSS feed from per-episode sidecars — no external credential, no network
(owned infra, like the blog adapter). These tests drive the real filesystem and assert the feed is
well-formed XML with the episode enclosure; re-publish is idempotent and delete prunes + rebuilds.
"""

import json
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.channels.base import get_adapter
from app.channels.podcast import PodcastAdapter
from app.config import settings
from app.models import Channel, ChannelType, ContentItem, ContentItemStatus, LifecycleState, Product


@pytest.fixture
def session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(settings, "workspace_root", str(root))
    return root


def _product(session, *, slug="live", domain="acme.example"):
    p = Product(
        name="Acme",
        slug=slug,
        lifecycle_state=LifecycleState.LIVE,
        marketing_domain=domain,
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _channel(session, product_id):
    c = Channel(product_id=product_id, type=ChannelType.PODCAST, enabled=True, autonomous=True)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _episode(session, workspace, product, *, title="Episode One", audio=b"ID3fake-mp3-bytes"):
    """Persist a critic_passed podcast item whose media_ref points at a real MP3 in workspace."""
    rel = f"{product.slug}/media/podcast/job-1/episode.mp3"
    path = workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio)
    item = ContentItem(
        product_id=product.id,
        channel_id=1,
        content_type="podcast",
        status=ContentItemStatus.CRITIC_PASSED,
        title=title,
        body="Show notes body.",
        meta_json=json.dumps({"description": "A great episode.", "podcast_dir": "x"}),
        media_ref=rel,
        created_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def _feed_root(workspace, product) -> ET.Element:
    feed = workspace / product.slug / "site" / "podcast" / "feed.xml"
    return ET.fromstring(feed.read_text())


def test_registered_for_podcast_channel():
    adapter = get_adapter(ChannelType.PODCAST)
    assert isinstance(adapter, PodcastAdapter)
    assert adapter.credential_key is None


def test_publish_writes_audio_page_and_feed(session, workspace):
    p = _product(session)
    c = _channel(session, p.id)
    item = _episode(session, workspace, p)

    result = PodcastAdapter().publish(item, p, c, None)

    podcast_dir = workspace / p.slug / "site" / "podcast"
    files = {f.name for f in podcast_dir.iterdir()}
    assert "feed.xml" in files
    assert any(n.endswith(".mp3") for n in files)
    assert any(n.endswith(".html") for n in files)
    assert result.external_url.endswith(".html")
    assert "acme.example" in result.external_url

    # The copied audio matches the source episode bytes.
    mp3 = next(podcast_dir.glob("*.mp3"))
    assert mp3.read_bytes() == b"ID3fake-mp3-bytes"


def test_feed_is_well_formed_with_enclosure(session, workspace):
    p = _product(session)
    c = _channel(session, p.id)
    item = _episode(session, workspace, p, title="Episode One")

    PodcastAdapter().publish(item, p, c, None)
    root = _feed_root(workspace, p)  # raises if not well-formed XML

    assert root.tag == "rss"
    channel = root.find("channel")
    assert channel.find("title").text == "Acme Podcast"
    entries = channel.findall("item")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.find("title").text == "Episode One"
    enclosure = entry.find("enclosure")
    assert enclosure.get("type") == "audio/mpeg"
    assert enclosure.get("url").endswith(".mp3")
    assert int(enclosure.get("length")) == len(b"ID3fake-mp3-bytes")
    assert entry.find("guid").text == f"sme-podcast-{item.id}"


def test_republish_same_item_is_idempotent(session, workspace):
    p = _product(session)
    c = _channel(session, p.id)
    item = _episode(session, workspace, p)

    r1 = PodcastAdapter().publish(item, p, c, None)
    r2 = PodcastAdapter().publish(item, p, c, None)

    assert r1.external_url == r2.external_url
    root = _feed_root(workspace, p)
    assert len(root.find("channel").findall("item")) == 1  # not duplicated
    # Exactly one mp3 for the one item.
    assert len(list((workspace / p.slug / "site" / "podcast").glob("*.mp3"))) == 1


def test_two_episodes_both_appear_newest_first(session, workspace):
    p = _product(session)
    c = _channel(session, p.id)
    older = _episode(session, workspace, p, title="Older")
    # A second episode with a later pubDate and its own media file.
    rel2 = f"{p.slug}/media/podcast/job-2/episode.mp3"
    (workspace / rel2).parent.mkdir(parents=True, exist_ok=True)
    (workspace / rel2).write_bytes(b"ID3second")
    newer = ContentItem(
        product_id=p.id,
        channel_id=c.id,
        content_type="podcast",
        status=ContentItemStatus.CRITIC_PASSED,
        title="Newer",
        body="notes",
        meta_json=json.dumps({"description": "Newer ep."}),
        media_ref=rel2,
        created_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
    )
    session.add(newer)
    session.commit()
    session.refresh(newer)

    PodcastAdapter().publish(older, p, c, None)
    PodcastAdapter().publish(newer, p, c, None)

    titles = [
        e.find("title").text for e in _feed_root(workspace, p).find("channel").findall("item")
    ]
    assert titles == ["Newer", "Older"]  # newest first


def test_delete_prunes_episode_and_rebuilds_feed(session, workspace):
    p = _product(session)
    c = _channel(session, p.id)
    item = _episode(session, workspace, p)

    result = PodcastAdapter().publish(item, p, c, None)
    PodcastAdapter().delete(result.external_url, p, c, None)

    podcast_dir = workspace / p.slug / "site" / "podcast"
    assert not list(podcast_dir.glob("*.mp3"))  # audio pruned
    assert not list(podcast_dir.glob("episode*.json")) and not list(podcast_dir.glob("*-*.json"))
    assert len(_feed_root(workspace, p).find("channel").findall("item")) == 0  # feed rebuilt empty


def test_publish_without_media_ref_fails_loudly(session, workspace):
    p = _product(session)
    c = _channel(session, p.id)
    item = ContentItem(
        product_id=p.id,
        channel_id=c.id,
        content_type="podcast",
        status=ContentItemStatus.CRITIC_PASSED,
        title="No audio",
        body="notes",
        media_ref=None,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    with pytest.raises(RuntimeError, match="media_ref"):
        PodcastAdapter().publish(item, p, c, None)
