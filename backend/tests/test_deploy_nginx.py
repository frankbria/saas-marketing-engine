"""S0.5: what `deploy_site` must put in a generated vhost, and when it reloads nginx (issue #80).

Three contracts, each of which has a specific way of failing silently in production:

1. **The ACME include.** #78 added `location ~ /\\. { deny all; }` to every generated vhost. That
   regex matches any path containing `/.` — including `/.well-known/acme-challenge/`. Without a
   `^~` prefix location taking precedence, certbot's challenge is denied, validation times out,
   and the site stays HTTP-only with nothing in the app logs.
2. **The public-API include.** One uvicorn process serves both API surfaces and v1 has no auth on
   the private one, so nginx is the only thing keeping `/api/private/*` off the internet.
3. **The TLS wildcard include.** `deploy_site` rewrites `<domain>.conf` on every `setup_site` run.
   Anything certbot's `--nginx` plugin wrote into that file would be silently erased on the next
   run, leaving a valid certificate nobody serves. A wildcard include of a directory certbot's
   deploy hook owns survives regeneration — and a wildcard that matches nothing is legal nginx,
   so a domain without TLS still loads.

Plus the reload seam: writing a vhost nginx has not re-read changes nothing on the wire.
"""

from __future__ import annotations

import subprocess

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.ai.client import SiteContent
from app.config import settings
from app.models import LifecycleState, Product
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
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "ws"))
    monkeypatch.setattr(settings, "nginx_sites_root", str(tmp_path / "nginx"))
    monkeypatch.setattr(settings, "nginx_snippets_root", str(tmp_path / "snippets"))
    monkeypatch.setattr(settings, "nginx_reload_command", "")
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


def _product(session) -> Product:
    product = Product(
        name="Acme", slug="acme", lifecycle_state=LifecycleState.LIVE, marketing_domain=DOMAIN
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    create_workspace(product.slug)
    return product


def _deploy(session, workspace) -> str:
    product = _product(session)
    site_dir = site_mod.build_site(product, _content())
    site_mod.deploy_site(product, site_dir)
    return (workspace / "nginx" / f"{DOMAIN}.conf").read_text()


# --- includes ----------------------------------------------------------------


def test_vhost_includes_the_acme_challenge_snippet(session, workspace):
    """Without this the dotfile deny from #78 blocks every certificate issuance."""
    vhost = _deploy(session, workspace)
    assert f"include {workspace / 'snippets'}/sme-acme.conf;" in vhost


def test_acme_include_precedes_the_dotfile_deny(session, workspace):
    """Belt-and-braces on ordering. nginx resolves a matching `^~` prefix location before any
    regex location regardless of file order, so this is not what makes the snippet win — but a
    future edit that moved the include below the deny would be a strong smell, and the ordering
    documents the intent for whoever reads the generated file."""
    vhost = _deploy(session, workspace)
    assert vhost.index("sme-acme.conf") < vhost.index("location ~ /\\. { deny all; }")


def test_vhost_includes_the_public_api_allowlist(session, workspace):
    vhost = _deploy(session, workspace)
    assert f"include {workspace / 'snippets'}/sme-public-api.conf;" in vhost


def test_vhost_includes_a_per_domain_tls_wildcard(session, workspace):
    """A wildcard, not a fixed filename: `include .../foo.conf` on a missing file is a hard nginx
    error, so a domain without a certificate would fail to load at all. A wildcard matching
    nothing is legal, which is what lets one generated vhost serve both the pre-TLS and post-TLS
    states without `deploy_site` knowing which it is."""
    vhost = _deploy(session, workspace)
    assert f"include {workspace / 'snippets'}/sme-tls/{DOMAIN}/*.conf;" in vhost


def test_regenerating_the_vhost_preserves_the_tls_snippet(session, workspace, tmp_path):
    """The regression this design exists to prevent: certbot's own nginx plugin would write into
    the generated file, and the next `setup_site` run would erase it."""
    tls_dir = tmp_path / "snippets" / "sme-tls" / DOMAIN
    tls_dir.mkdir(parents=True)
    (tls_dir / "tls.conf").write_text("listen 443 ssl;\n", encoding="utf-8")

    _deploy(session, workspace)  # regenerate the vhost from scratch

    assert (tls_dir / "tls.conf").read_text() == "listen 443 ssl;\n"


# --- reload seam -------------------------------------------------------------


def test_no_reload_command_configured_is_a_silent_no_op(session, workspace, monkeypatch):
    """Dev and tests must not shell out. The empty default is what keeps `setup_site` runnable on
    a laptop with no nginx at all."""
    calls: list[list[str]] = []
    monkeypatch.setattr(site_mod.subprocess, "run", lambda *a, **k: calls.append(a))
    _deploy(session, workspace)
    assert calls == []


def test_reload_command_runs_after_the_vhost_is_written(session, workspace, monkeypatch):
    """Ordering matters: reloading before the write would make nginx re-read the *old* config and
    report success while serving nothing new."""
    monkeypatch.setattr(settings, "nginx_reload_command", "sudo /usr/local/sbin/sme-nginx-reload")
    seen: dict[str, object] = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["vhost_existed"] = (workspace / "nginx" / f"{DOMAIN}.conf").exists()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(site_mod.subprocess, "run", _fake_run)
    _deploy(session, workspace)

    assert seen["cmd"] == ["sudo", "/usr/local/sbin/sme-nginx-reload"]
    assert seen["vhost_existed"] is True


def test_a_failing_reload_raises_rather_than_passing_silently(session, workspace, monkeypatch):
    """A vhost nginx never re-read is a site that 404s. If the reload fails — a bad config, a
    missing sudoers grant — `setup_site` must fail so the job records the error, instead of
    reporting a deployed site that is not being served."""
    monkeypatch.setattr(settings, "nginx_reload_command", "sudo /usr/local/sbin/sme-nginx-reload")

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "nginx: configuration file test failed")

    monkeypatch.setattr(site_mod.subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError, match="nginx reload failed"):
        _deploy(session, workspace)


def test_reload_failure_still_leaves_the_vhost_on_disk(session, workspace, monkeypatch):
    """The vhost is the durable artifact; the reload is the activation. Leaving the file lets the
    operator fix the cause and run `nginx-reload` by hand, rather than re-running the whole
    (token-spending) setup_site job."""
    monkeypatch.setattr(settings, "nginx_reload_command", "sudo /usr/local/sbin/sme-nginx-reload")
    monkeypatch.setattr(
        site_mod.subprocess,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "boom"),
    )

    with pytest.raises(RuntimeError):
        _deploy(session, workspace)

    assert (workspace / "nginx" / f"{DOMAIN}.conf").exists()
