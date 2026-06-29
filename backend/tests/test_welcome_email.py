"""S2.4: welcome email — one best-effort send per captured lead, no drip engine.

The lead row itself is already covered by test_public_funnel; here we assert the welcome
email is scheduled on capture and that the stdlib SMTP sender is no-op/safe by construction.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app import workspace
from app.api.public import ratelimit
from app.api.public.funnel import get_welcome_sender
from app.db import get_session
from app.integrations import email as email_mod
from app.main import create_app
from app.models.product import Product


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(workspace.settings, "workspace_root", str(tmp_path / "ws"))
    ratelimit.reset()

    def _session_override():
        with Session(engine) as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = _session_override
    with TestClient(app) as c:
        yield c, app


def _make_product(client: TestClient, *, name="Auto Author") -> str:
    resp = client.post("/api/private/products", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["slug"]


def test_lead_schedules_welcome_email(ctx):
    client, app = ctx
    sent: list[tuple[str, str]] = []
    app.dependency_overrides[get_welcome_sender] = lambda: (
        lambda to, product: sent.append((to, product.name))
    )
    slug = _make_product(client, name="Auto Author")

    resp = client.post(f"/api/funnel/{slug}/lead", json={"email": "User@Example.com"})

    assert resp.status_code == 201
    # background task ran; recipient normalized; product carried through
    assert sent == [("user@example.com", "Auto Author")]


def test_visit_does_not_send_welcome(ctx):
    client, app = ctx
    sent: list = []
    app.dependency_overrides[get_welcome_sender] = lambda: (lambda to, product: sent.append(to))
    slug = _make_product(client)

    resp = client.post(f"/api/funnel/{slug}/visit", json={})

    assert resp.status_code == 201
    assert sent == []


# --- send_welcome (stdlib SMTP) ---------------------------------------------


class _FakeSMTP:
    """Records the EmailMessage handed to send_message; no network."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.started_tls = False
        self.login_args = None
        self.sent = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True
        self.tls_context = context

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        self.sent = msg


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeSMTP.instances = []


def _product() -> Product:
    return Product(id=1, name="Auto Author", slug="auto-author")


def test_send_welcome_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(email_mod.settings, "smtp_host", None)
    # If SMTP were attempted, this would explode — proves the no-op path.
    monkeypatch.setattr(email_mod.smtplib, "SMTP", lambda *a, **k: 1 / 0)

    email_mod.send_welcome("lead@example.com", _product())  # must not raise

    assert _FakeSMTP.instances == []


def test_send_welcome_builds_and_sends(monkeypatch):
    monkeypatch.setattr(email_mod.settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(email_mod.settings, "smtp_port", 587)
    monkeypatch.setattr(email_mod.settings, "smtp_from", "hello@autoauthor.app")
    monkeypatch.setattr(email_mod.settings, "smtp_user", "apikey")

    class _Secret:
        def get_secret_value(self):
            return "pw"

    monkeypatch.setattr(email_mod.settings, "smtp_password", _Secret())
    monkeypatch.setattr(email_mod.smtplib, "SMTP", _FakeSMTP)

    email_mod.send_welcome("lead@example.com", _product())

    assert len(_FakeSMTP.instances) == 1
    smtp = _FakeSMTP.instances[0]
    assert smtp.started_tls is True
    assert smtp.tls_context is not None  # verified context, not the default unverified one
    assert smtp.login_args == ("apikey", "pw")
    msg = smtp.sent
    assert msg["To"] == "lead@example.com"
    assert msg["From"] == "hello@autoauthor.app"
    assert "Auto Author" in msg["Subject"]
    assert "Auto Author" in msg.get_content()


def test_send_welcome_swallows_smtp_failure(monkeypatch):
    monkeypatch.setattr(email_mod.settings, "smtp_host", "smtp.example.com")

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(email_mod.smtplib, "SMTP", _boom)

    # best-effort: a down SMTP server must not propagate out of the background task
    email_mod.send_welcome("lead@example.com", _product())


def test_send_welcome_swallows_bad_header(monkeypatch):
    # A CR/LF in product.name makes EmailMessage raise on Subject assignment — best-effort must
    # swallow that too (it happens before any transport), so SMTP is never even dialed.
    monkeypatch.setattr(email_mod.settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(email_mod.smtplib, "SMTP", _FakeSMTP)
    bad = Product(id=1, name="Auto\r\nBcc: evil@example.com", slug="x")

    email_mod.send_welcome("lead@example.com", bad)  # must not raise

    assert _FakeSMTP.instances == []  # failed before opening a connection
