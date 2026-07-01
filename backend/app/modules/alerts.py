"""Operator alerts (TECH_SPEC §8.4).

A single choke-point for "something the operator must see" signals. v1 emits a structured WARNING
log line (grep-able, and the vault redactor already scrubs secrets from it); S6.2 (heartbeat digest
+ delivery: email/webhook) extends *this* function rather than scattering alert calls.

ponytail: log-only for v1; S6.2 wires real delivery here. No Alert table yet — inventing S6.2's
schema now would be speculative (YAGNI).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("app.alerts")


def raise_alert(kind: str, message: str, **context: object) -> None:
    """Emit an operator alert. `kind` is a stable machine tag (e.g. ``oauth_refresh_failed``)."""
    ctx = " ".join(f"{k}={v}" for k, v in context.items())
    logger.warning("ALERT %s: %s%s", kind, message, f" [{ctx}]" if ctx else "")
