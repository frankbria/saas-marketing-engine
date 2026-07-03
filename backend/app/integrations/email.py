"""Welcome email (S2.4) — stdlib smtplib, best-effort, one send per captured lead.

No `email`/ESP SDK and no new runtime dependency — `smtplib` + `EmailMessage` send a single
transactional message, mirroring the no-SDK choice in stripe_api/the webhook. Called on a FastAPI
BackgroundTask (see funnel.py): the lead row is the asset, so a slow or down SMTP server must never
block or fail lead capture — send errors are caught and logged, not raised.

ponytail: plaintext body, no drip/sequence engine, no retry/queue/bounce-handling in v1. Add a
templated/HTML body when brand copy lands; add a queue when delivery reliability matters.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.config import settings
from app.models.product import Product
from app.secrets.vault import redact

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30


def _mask(email: str) -> str:
    """Redact a lead email for logs — keep enough to debug, not the raw PII recipient."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def _body(product: Product) -> str:
    return f"Thanks for your interest in {product.name}!\n\nWe'll be in touch shortly. — The team\n"


def _smtp_send(msg: EmailMessage) -> None:
    """Transport one message over the configured SMTP server. Raises on failure — each caller
    wraps this in its own best-effort guard with caller-specific logging."""
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=_TIMEOUT_SECONDS) as smtp:
        if settings.smtp_starttls:
            # Verified context — credentials must not travel over a spoofable TLS session.
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
        smtp.send_message(msg)


def send_welcome(to: str, product: Product) -> None:
    """Send one welcome email to a freshly-captured lead. No-op if SMTP is unconfigured."""
    if not settings.smtp_host:
        logger.info("SMTP not configured (SME_SMTP_HOST); skipping welcome email to %s", _mask(to))
        return

    try:
        # Inside the guard: a CR/LF in product.name makes EmailMessage raise on header assignment,
        # and best-effort must swallow that too (not just transport errors).
        msg = EmailMessage()
        msg["From"] = settings.smtp_from or settings.smtp_user or "no-reply@localhost"
        msg["To"] = to
        msg["Subject"] = f"Welcome to {product.name}"
        msg.set_content(_body(product))
        _smtp_send(msg)
    except Exception:  # best-effort: delivery failure must not break lead capture
        logger.exception("welcome email to %s failed", _mask(to))


def send_alert_email(to: str, kind: str, message: str) -> None:
    """One operator alert email (S6.2). No-op if SMTP is unconfigured; best-effort otherwise —
    alert delivery failing must never break the pipeline that raised the alert."""
    if not settings.smtp_host:
        logger.info("SMTP not configured (SME_SMTP_HOST); skipping alert email (%s)", kind)
        return

    try:
        msg = EmailMessage()
        msg["From"] = settings.smtp_from or settings.smtp_user or "no-reply@localhost"
        msg["To"] = to
        msg["Subject"] = f"[SME alert] {kind}"
        # The vault's log-record redaction can't see email bodies — alert context can carry raw
        # provider error strings (e.g. OAuth refresh failures), so scrub here at the boundary.
        msg.set_content(f"{redact(message)}\n")
        _smtp_send(msg)
    except Exception:
        logger.exception("alert email (%s) failed", kind)


def _digest_body(digest: dict, alerts: list[dict]) -> str:
    lines = ["Heartbeat digest (last 24h):", ""]
    for row in digest["channels"]:
        lines.append(
            f"  {row['channel_type']}: published={row['published']} "
            f"failed={row['failed']} reach={row['reach']}"
        )
    lines.append("")
    if alerts:
        lines.append("Alerts:")
        lines.extend(f"  [{a['kind']}] {a['message']}" for a in alerts)
    else:
        lines.append("No alerts.")
    return "\n".join(lines) + "\n"


def send_digest(to: str, product: Product, digest: dict, alerts: list[dict]) -> None:
    """Daily heartbeat digest email (S6.2). No-op if SMTP is unconfigured; best-effort otherwise."""
    if not settings.smtp_host:
        logger.info(
            "SMTP not configured (SME_SMTP_HOST); skipping digest email for product %s", product.id
        )
        return

    try:
        msg = EmailMessage()
        msg["From"] = settings.smtp_from or settings.smtp_user or "no-reply@localhost"
        msg["To"] = to
        msg["Subject"] = f"[SME heartbeat] {product.name}: {len(alerts)} alert(s)"
        # Defense in depth: alert messages fold into the digest body too — scrub at the boundary.
        msg.set_content(redact(_digest_body(digest, alerts)))
        _smtp_send(msg)
    except Exception:
        logger.exception("digest email for product %s failed", product.id)
