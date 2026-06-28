"""S0.4: Fernet credentials vault — encrypt/decrypt, ciphertext-at-rest, log redaction."""

import logging

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine

from app.secrets import vault

# A real Fernet key (generated once) so tests don't depend on the deploy env var.
TEST_KEY = "dx1Rl47HVpUJ4gGYU3IM4x05YTyFhJbRSMn8h2RsbFQ="
SECRET = "sk_live_super_secret_token_value"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(vault.settings, "vault_key", TEST_KEY)
    yield


@pytest.fixture
def session(tmp_path):
    db = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_encrypt_decrypt_roundtrip():
    token = vault.encrypt(SECRET)
    assert token != SECRET
    assert vault.decrypt(token) == SECRET


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(vault.settings, "vault_key", None)
    with pytest.raises(RuntimeError, match="SME_VAULT_KEY"):
        vault.encrypt(SECRET)


def test_put_get_credential_roundtrip(session):
    cred = vault.put_credential(session, product_id=1, key="stripe", plaintext=SECRET)
    assert cred.id is not None
    assert vault.get_credential(session, product_id=1, key="stripe") == SECRET


def test_only_ciphertext_at_rest(session):
    vault.put_credential(session, product_id=1, key="stripe", plaintext=SECRET)
    # Read the raw column straight from SQLite — no ORM decryption in the path.
    rows = session.exec(text("SELECT ciphertext FROM credential")).all()
    assert rows, "credential row not persisted"
    ciphertext = rows[0][0]
    assert SECRET not in ciphertext
    assert vault.decrypt(ciphertext) == SECRET


def test_get_credential_missing_returns_none(session):
    assert vault.get_credential(session, product_id=99, key="nope") is None


def test_secret_redacted_from_logs(caplog):
    vault.install_redaction()
    vault.encrypt(SECRET)  # registers the plaintext for redaction
    logger = logging.getLogger("test.leak")
    with caplog.at_level(logging.INFO):
        logger.info("about to publish with token=%s", SECRET)
        logger.info(f"f-string leak {SECRET}")
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET not in blob
    assert "***" in blob
