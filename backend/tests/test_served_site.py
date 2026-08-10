"""S4.5.1: published content is reachable on the served site (issue #78).

The defect: the crank's filesystem-backed adapters (blog, podcast) write into the product's
workspace site tree, while nginx served a *copy* made once at setup time — so every autonomously
published artifact 404'd and every recorded `external_url` pointed at nothing.

The fix makes the workspace site tree *be* the served root: `deploy_site` emits a vhost rooted
there instead of copying. These tests pin the contract end to end — deploy, publish, retract —
because a unit test on `deploy_site` alone would not have caught the original bug (each half was
individually correct; only the seam between them was broken).

Real filesystem, no network: both adapters are owned-infra and need no credential.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.ai.client import SiteContent
from app.channels.blog import BlogAdapter
from app.channels.podcast import PodcastAdapter
from app.config import settings
from app.models import Channel, ChannelType, ContentItem, ContentItemStatus, LifecycleState, Product
from app.modules.crank.retract import retract_item
from app.modules.setup import site as site_mod
from app.workspace import create_workspace

DOMAIN = "acme.example"


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
    """Isolate workspace + nginx roots so build/deploy hit tmp dirs, not the repo."""
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "ws"))
    monkeypatch.setattr(settings, "nginx_sites_root", str(tmp_path / "nginx"))
    monkeypatch.setattr(settings, "public_api_base_url", "https://api.example.com")
    return tmp_path


def _content() -> SiteContent:
    return SiteContent(
        headline="Write your book",
        subhead="Faster than you think",
        value_props=["Outline in minutes"],
        cta_label="Start",
        primary_color="#112233",
        accent_color="#445566",
        font_family="Georgia, serif",
    )


def _product(session, *, slug="acme", domain=DOMAIN) -> Product:
    product = Product(
        name="Acme",
        slug=slug,
        lifecycle_state=LifecycleState.LIVE,
        marketing_domain=domain,
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    create_workspace(product.slug)
    return product


def _channel(session, product_id, channel_type) -> Channel:
    channel = Channel(product_id=product_id, type=channel_type, enabled=True, autonomous=True)
    session.add(channel)
    session.commit()
    session.refresh(channel)
    return channel


def _deploy(product):
    """build + deploy, returning the directory nginx is rooted at."""
    return site_mod.deploy_site(product, site_mod.build_site(product, _content()))


def _served_path(served_root, external_url, *, suffix=""):
    """Map a published `external_url` back to the file nginx would serve for it."""
    return served_root / (urlsplit(external_url).path.lstrip("/") + suffix)


def _blog_item(session, product, channel, *, slug="my-post", title="My Post") -> ContentItem:
    item = ContentItem(
        product_id=product.id,
        channel_id=channel.id,
        content_type="blog",
        status=ContentItemStatus.SCHEDULED,
        title=title,
        body="# Body\n\nSome article text.",
        meta_json=json.dumps({"slug": slug}),
        idempotency_key=f"blog:{slug}",
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def _podcast_item(
    session,
    product,
    channel,
    *,
    title="Episode One",
    job="job-1",
    scheduled_for=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
) -> ContentItem:
    rel = f"{product.slug}/media/podcast/{job}/episode.mp3"
    path = Path(settings.workspace_root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ID3fake-mp3-bytes")
    item = ContentItem(
        product_id=product.id,
        channel_id=channel.id,
        content_type="podcast",
        status=ContentItemStatus.SCHEDULED,
        title=title,
        body="Show notes body.",
        meta_json=json.dumps({"description": "A great episode."}),
        media_ref=rel,
        scheduled_for=scheduled_for,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


# ---- deploy contract: the workspace tree IS the served root --------------------------------


def test_vhost_is_rooted_at_the_workspace_site_tree(session, workspace):
    product = _product(session)
    served_root = _deploy(product)

    site_dir = (workspace / "ws" / product.slug / "site").resolve()
    assert served_root == site_dir
    assert (served_root / "index.html").is_file()

    vhost = (workspace / "nginx" / f"{DOMAIN}.conf").read_text()
    assert f"server_name {DOMAIN};" in vhost
    assert f"root {site_dir};" in vhost


def test_vhost_root_is_absolute_even_when_workspace_root_is_relative(
    session, workspace, tmp_path, monkeypatch
):
    """`workspace_root` defaults to a *relative* './workspace'. nginx resolves a relative `root`
    against its own prefix (/etc/nginx), not this process's cwd — emitting it verbatim would 404
    every published URL, re-creating #78 one layer down. The fixture's absolute tmp_path hides
    this, so drive it from a genuinely relative setting with cwd moved to a temp dir."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "workspace_root", "./relws")

    product = _product(session)
    served_root = _deploy(product)

    assert served_root.is_absolute()
    vhost = (workspace / "nginx" / f"{DOMAIN}.conf").read_text()
    root_line = next(ln.strip() for ln in vhost.splitlines() if ln.strip().startswith("root "))
    assert "./" not in root_line, root_line
    assert root_line == f"root {served_root};"


def test_deploy_refuses_a_site_dir_outside_the_workspace_root(session, workspace):
    """`site_dir` embeds `product.slug`. Slugs are sanitised at creation, but the document root is
    too dangerous to rely on that alone — a traversing slug must be rejected at the point of use."""
    product = _product(session)
    escaped = Path(settings.workspace_root).resolve().parent / "elsewhere" / "site"

    with pytest.raises(RuntimeError, match="escapes the workspace root"):
        site_mod.deploy_site(product, escaped)


def test_vhost_hardens_the_app_mutated_document_root(session, workspace):
    """The served tree is written by the engine at runtime, so the vhost blocks symlink escapes and
    the in-flight atomic-write sidecars, and hard-404s published content instead of falling back to
    the landing page (a retracted post must not keep returning 200)."""
    product = _product(session)
    _deploy(product)

    vhost = (workspace / "nginx" / f"{DOMAIN}.conf").read_text()
    assert "disable_symlinks on;" in vhost
    assert "location ~ /\\. { deny all; }" in vhost
    assert "location ~ \\.tmp$ { deny all; }" in vhost
    assert "location /blog/ { try_files $uri $uri.html =404; }" in vhost
    assert "location /podcast/ { try_files $uri =404; }" in vhost


def test_deploy_copies_nothing_into_the_nginx_root(session, workspace):
    """Regression: the old deploy copied the site tree, and that copy went stale on every
    publish. The nginx root must hold vhost config only."""
    product = _product(session)
    _deploy(product)

    nginx_root = workspace / "nginx"
    assert sorted(p.name for p in nginx_root.iterdir()) == [f"{DOMAIN}.conf"]


def test_vhost_resolves_extensionless_post_urls(session, workspace):
    """BlogAdapter returns `/blog/<slug>` while the file on disk is `<slug>.html`; without a
    `$uri.html` try_files entry every post URL falls through to the landing page (a soft 404)."""
    product = _product(session)
    _deploy(product)

    vhost = (workspace / "nginx" / f"{DOMAIN}.conf").read_text()
    assert "$uri.html" in vhost


def test_private_workspace_dirs_are_not_inside_the_served_root(session, workspace):
    """Serving the workspace tree makes `site/` world-readable, so everything private must stay a
    *sibling* of it, never underneath. Pins the two that exist today:

      workspace/<slug>/vault/  — Fernet-encrypted credentials (S0.4)
      workspace/<slug>/media/  — render checkpoints, narration MP3s, unpublished cuts (S5.1/S5.2)

    A future layout change that moves either under `site/` would publish secrets or unreleased
    media to the internet; this test is the tripwire.
    """
    product = _product(session)
    served_root = _deploy(product)

    product_root = workspace / "ws" / product.slug
    assert served_root == product_root / "site"

    vault = product_root / "vault"
    assert vault.is_dir()  # created by create_workspace
    for private in (vault, product_root / "media"):
        assert served_root not in private.parents
        assert not private.is_relative_to(served_root)


# ---- blog: publish → reachable → retract → gone ---------------------------------------------


def test_published_blog_post_is_reachable_at_its_external_url(session, workspace):
    product = _product(session)
    channel = _channel(session, product.id, ChannelType.BLOG)
    served_root = _deploy(product)
    item = _blog_item(session, product, channel)

    result = BlogAdapter().publish(item, product, channel, None)

    served = _served_path(served_root, result.external_url, suffix=".html")
    assert served.is_file(), f"{result.external_url} does not resolve to a served file"
    assert "Some article text." in served.read_text()


def test_retracted_blog_post_is_removed_from_the_served_root(session, workspace):
    product = _product(session)
    channel = _channel(session, product.id, ChannelType.BLOG)
    served_root = _deploy(product)
    item = _blog_item(session, product, channel)

    result = BlogAdapter().publish(item, product, channel, None)
    served = _served_path(served_root, result.external_url, suffix=".html")
    assert served.is_file()

    item.status = ContentItemStatus.PUBLISHED
    item.external_url = result.external_url
    session.add(item)
    session.commit()
    retract_item(session, item)

    assert not served.exists()


def test_publishing_a_post_does_not_disturb_the_landing_page(session, workspace):
    """The old model rebuilt the served root wholesale; a per-publish sync must not touch
    index.html (nor briefly remove it)."""
    product = _product(session)
    channel = _channel(session, product.id, ChannelType.BLOG)
    served_root = _deploy(product)
    before = (served_root / "index.html").read_text()

    BlogAdapter().publish(_blog_item(session, product, channel), product, channel, None)

    assert (served_root / "index.html").read_text() == before


# ---- podcast: episode audio + feed reachable ------------------------------------------------


def test_published_episode_and_feed_are_reachable_under_the_served_root(session, workspace):
    product = _product(session)
    channel = _channel(session, product.id, ChannelType.PODCAST)
    served_root = _deploy(product)
    item = _podcast_item(session, product, channel)

    result = PodcastAdapter().publish(item, product, channel, None)

    episode_page = _served_path(served_root, result.external_url)
    assert episode_page.is_file(), f"{result.external_url} does not resolve to a served file"

    feed = served_root / "podcast" / "feed.xml"
    assert feed.is_file()

    root = ET.fromstring(feed.read_text())
    enclosures = root.findall(".//item/enclosure")
    assert len(enclosures) == 1
    audio_url = enclosures[0].attrib["url"]
    assert _served_path(served_root, audio_url).is_file(), f"{audio_url} is not served"


def _publish_episode(session, product, channel, item):
    """Publish an episode and mark it published, as the crank's publish pass would.

    `retract_item` requires `published` status and an `external_url` — mirroring the blog test.
    """
    result = PodcastAdapter().publish(item, product, channel, None)
    item.status = ContentItemStatus.PUBLISHED
    item.external_url = result.external_url
    session.add(item)
    session.commit()
    return result


def _feed_enclosure_urls(served_root) -> list[str]:
    """Enclosure URLs advertised by the served feed. Parsing doubles as a well-formedness check —
    `ET.fromstring` raises on malformed XML, which is what a broken rebuild would emit."""
    feed = served_root / "podcast" / "feed.xml"
    assert feed.is_file(), "feed.xml is missing from the served root"
    return [
        enclosure.attrib["url"]
        for enclosure in ET.fromstring(feed.read_text()).findall(".//item/enclosure")
    ]


def test_retracted_episode_is_removed_from_the_served_root_and_the_feed(session, workspace):
    """S4.5.2 (#86): the delete-time feed rebuild is the uncovered half of podcast retract.

    Unlinking the MP3 is the easy part and was already covered. The part with no served-root test
    is `_rebuild_feed` running *on delete*: break its glob or its sort and the audio file is gone
    while `feed.xml` still advertises the `<enclosure>`. Podcast clients then retry a dead URL
    forever, and the dashboard shows a clean, successful retract. Assert on the feed, not just the
    files, or the regression is invisible.
    """
    product = _product(session)
    channel = _channel(session, product.id, ChannelType.PODCAST)
    served_root = _deploy(product)

    keeper = _podcast_item(session, product, channel, title="Keeper", job="job-1")
    doomed = _podcast_item(
        session,
        product,
        channel,
        title="Doomed",
        job="job-2",
        scheduled_for=datetime(2026, 7, 8, 12, 0, tzinfo=UTC),
    )
    _publish_episode(session, product, channel, keeper)
    doomed_result = _publish_episode(session, product, channel, doomed)

    assert len(_feed_enclosure_urls(served_root)) == 2

    # The three artifacts the adapter writes, all under the served root.
    doomed_page = _served_path(served_root, doomed_result.external_url)
    doomed_slug = doomed_page.name.removesuffix(".html")
    doomed_audio = served_root / "podcast" / f"{doomed_slug}.mp3"
    doomed_sidecar = served_root / "podcast" / f"{doomed_slug}.json"
    assert doomed_page.is_file() and doomed_audio.is_file() and doomed_sidecar.is_file()

    retract_item(session, doomed)

    # 1. every artifact is gone from the served root
    assert not doomed_page.exists()
    assert not doomed_audio.exists()
    assert not doomed_sidecar.exists()

    # 2. the feed no longer advertises it — the assertion the suite was missing
    remaining = _feed_enclosure_urls(served_root)
    assert not any(
        doomed_slug in url for url in remaining
    ), f"feed.xml still advertises the retracted episode: {remaining}"

    # 3. the rebuild pruned rather than truncated: the other episode survives, and its enclosure
    #    still resolves to a real file under the served root
    assert len(remaining) == 1
    assert _served_path(
        served_root, remaining[0]
    ).is_file(), f"surviving enclosure {remaining[0]} is not served"


def test_retracting_the_last_episode_leaves_a_well_formed_empty_feed(session, workspace):
    """The prune-to-nothing case: an empty feed is correct, a missing or malformed one is not.

    A subscribed client keeps polling feed.xml after the last episode is pulled; it must still
    parse, and it must still carry the channel metadata rather than being deleted outright.
    """
    product = _product(session)
    channel = _channel(session, product.id, ChannelType.PODCAST)
    served_root = _deploy(product)
    item = _podcast_item(session, product, channel)
    _publish_episode(session, product, channel, item)

    retract_item(session, item)

    assert _feed_enclosure_urls(served_root) == []
    root = ET.fromstring((served_root / "podcast" / "feed.xml").read_text())
    assert root.findtext("./channel/title") == f"{product.name} Podcast"
