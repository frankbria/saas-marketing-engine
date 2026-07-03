"""Operator alerts (TECH_SPEC §8.4).

A single choke-point for "something the operator must see" signals. Always emits a structured
WARNING log line (grep-able, and the vault redactor already scrubs secrets from it); S6.2 adds
email delivery on top when `alert_email_to` + SMTP are configured — unset, behavior stays log-only.
Delivery is best-effort inside the email helper: a down SMTP server must never break the pipeline
that raised the alert.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.integrations.email import send_alert_email

logger = logging.getLogger("app.alerts")


def raise_alert(kind: str, message: str, **context: object) -> None:
    """Emit an operator alert. `kind` is a stable machine tag (e.g. ``oauth_refresh_failed``)."""
    ctx = " ".join(f"{k}={v}" for k, v in context.items())
    logger.warning("ALERT %s: %s%s", kind, message, f" [{ctx}]" if ctx else "")
    if settings.alert_email_to:
        send_alert_email(settings.alert_email_to, kind, f"{message}{f' [{ctx}]' if ctx else ''}")
