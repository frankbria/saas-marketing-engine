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

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30


def _mask(email: str) -> str:
    """Redact a lead email for logs — keep enough to debug, not the raw PII recipient."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def _body(product: Product) -> str:
    return (
        f"Thanks for your interest in {product.name}!\n\n" "We'll be in touch shortly. — The team\n"
    )


def send_welcome(to: str, product: Product) -> None:
    """Send one welcome email to a freshly-captured lead. No-op if SMTP is unconfigured."""
    host = settings.smtp_host
    if not host:
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

        with smtplib.SMTP(host, settings.smtp_port, timeout=_TIMEOUT_SECONDS) as smtp:
            if settings.smtp_starttls:
                # Verified context — credentials must not travel over a spoofable TLS session.
                smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_user and settings.smtp_password:
                smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
            smtp.send_message(msg)
    except Exception:  # best-effort: delivery failure must not break lead capture
        logger.exception("welcome email to %s failed", _mask(to))
