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
import threading
from datetime import datetime

from cryptography.fernet import Fernet
from sqlmodel import Session, select

from app.config import settings
from app.models.credential import Credential

REDACTED = "***"
_secrets: set[str] = set()
_secrets_lock = threading.RLock()  # the worker loop logs from a background thread


def generate_key() -> str:
    """Ops helper: a fresh url-safe base64 Fernet key for `SME_VAULT_KEY`."""
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    key = settings.vault_key
    raw = key.get_secret_value() if hasattr(key, "get_secret_value") else key
    if not raw:
        raise RuntimeError("SME_VAULT_KEY is not set — cannot encrypt/decrypt secrets")
    return Fernet(raw.encode())


def register_secret(plaintext: str) -> None:
    """Mark a plaintext for redaction from all logs."""
    if plaintext:
        with _secrets_lock:
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
    commit: bool = True,
) -> Credential:
    """Encrypt and persist a secret. Only ciphertext hits the DB.

    `commit=False` stages the row without committing, so a caller writing several credentials plus a
    state change can flush them in one transaction (all-or-nothing) — used by the OAuth callback to
    avoid a half-applied connect if a later write fails.
    """
    cred = Credential(
        product_id=product_id,
        channel_id=channel_id,
        key=key,
        ciphertext=encrypt(plaintext),
        expires_at=expires_at,
    )
    session.add(cred)
    if commit:
        session.commit()
        session.refresh(cred)
    return cred


def get_credential(
    session: Session, product_id: int, key: str, *, channel_id: int | None = None
) -> str | None:
    """Decrypt the latest secret for (product_id, key, channel_id), or None if absent.

    `channel_id=None` matches product-level secrets (channel_id IS NULL), mirroring
    `put_credential` — so a per-channel token is never returned for a product-level
    lookup, and vice versa.
    """
    cred = session.exec(
        select(Credential)
        .where(
            Credential.product_id == product_id,
            Credential.key == key,
            Credential.channel_id == channel_id,
        )
        .order_by(Credential.id.desc())
    ).first()
    return decrypt(cred.ciphertext) if cred else None


def get_credential_expiry(
    session: Session, product_id: int, key: str, *, channel_id: int | None = None
) -> datetime | None:
    """The latest secret's `expires_at` for (product_id, key, channel_id), or None if absent/unset.

    Ciphertext is never touched — this reads only the expiry column, so it needs no decryption.
    """
    return session.exec(
        select(Credential.expires_at)
        .where(
            Credential.product_id == product_id,
            Credential.key == key,
            Credential.channel_id == channel_id,
        )
        .order_by(Credential.id.desc())
    ).first()


# --- log redaction -----------------------------------------------------------

_redaction_installed = False


def redact(text: str) -> str:
    """Scrub every registered secret from `text`. For content leaving the process by a
    non-log path (e.g. S6.2 alert/digest emails) — the log-record factory can't see those."""
    # Snapshot under the lock (the set may mutate from other threads) and replace
    # longest-first so an overlapping shorter secret can't leave part of a longer one.
    with _secrets_lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for s in secrets:
        text = text.replace(s, REDACTED)
    return text


def _redact_record(record: logging.LogRecord) -> None:
    with _secrets_lock:
        has_secrets = bool(_secrets)
    if not has_secrets:
        return
    msg = record.getMessage()
    redacted = redact(msg)
    if redacted != msg:
        record.msg = redacted
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
