"""Credentials vault (TECH_SPEC §9).

Symmetric Fernet encryption with a single global key from env `SME_VAULT_KEY`
(never stored in the DB); only ciphertext is persisted. Plaintext is never logged:
every value the vault touches is registered with a process-wide log-record factory
(`install_redaction`) that scrubs it from any log line — the runtime half of
"lint rule + log redaction" (§9). The static half is tests/test_no_plaintext_logging.py.

ponytail: one global key for v1 (single owner); per-product keys only if isolation
ever matters (§9).
"""

import logging
from datetime import datetime

from cryptography.fernet import Fernet
from sqlmodel import Session, select

from app.config import settings
from app.models.credential import Credential

REDACTED = "***"
_secrets: set[str] = set()


def generate_key() -> str:
    """Ops helper: a fresh url-safe base64 Fernet key for `SME_VAULT_KEY`."""
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    if not settings.vault_key:
        raise RuntimeError("SME_VAULT_KEY is not set — cannot encrypt/decrypt secrets")
    return Fernet(settings.vault_key.encode())


def register_secret(plaintext: str) -> None:
    """Mark a plaintext for redaction from all logs."""
    if plaintext:
        _secrets.add(plaintext)


def encrypt(plaintext: str) -> str:
    register_secret(plaintext)
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    plaintext = _fernet().decrypt(token.encode()).decode()
    register_secret(plaintext)
    return plaintext


def put_credential(
    session: Session,
    product_id: int,
    key: str,
    plaintext: str,
    *,
    channel_id: int | None = None,
    expires_at: datetime | None = None,
) -> Credential:
    """Encrypt and persist a secret. Only ciphertext hits the DB."""
    cred = Credential(
        product_id=product_id,
        channel_id=channel_id,
        key=key,
        ciphertext=encrypt(plaintext),
        expires_at=expires_at,
    )
    session.add(cred)
    session.commit()
    session.refresh(cred)
    return cred


def get_credential(session: Session, product_id: int, key: str) -> str | None:
    """Decrypt the latest secret for (product_id, key), or None if absent."""
    cred = session.exec(
        select(Credential)
        .where(Credential.product_id == product_id, Credential.key == key)
        .order_by(Credential.id.desc())
    ).first()
    return decrypt(cred.ciphertext) if cred else None


# --- log redaction -----------------------------------------------------------

_redaction_installed = False


def _redact_record(record: logging.LogRecord) -> None:
    if not _secrets:
        return
    msg = record.getMessage()
    if any(s in msg for s in _secrets):
        for s in _secrets:
            msg = msg.replace(s, REDACTED)
        record.msg = msg
        record.args = ()


class SecretRedactingFilter(logging.Filter):
    """Attach to a specific handler to scrub registered secrets from its records."""

    def filter(self, record: logging.LogRecord) -> bool:
        _redact_record(record)
        return True


def install_redaction() -> None:
    """Install a global log-record factory that scrubs every registered secret from
    any log line. Idempotent. A record factory (not a logger filter) is used so it
    catches records from every logger, including ad-hoc and test handlers.

    ponytail: bakes the redacted text into record.msg (loses lazy %-formatting) —
    fine for a single-owner internal tool; revisit if structured logging is added.
    """
    global _redaction_installed
    if _redaction_installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        _redact_record(record)
        return record

    logging.setLogRecordFactory(factory)
    _redaction_installed = True
