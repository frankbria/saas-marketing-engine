"""Ephemeral-GPU orchestration loop (S5.0, issue #28).

One APScheduler tick decides the 0↔1 worker state: pending `media` jobs + no live worker
→ boot a provider pod (unless the monthly spend cap says no); queue idle past the
threshold → tear it down and verify it's gone. Observations (queue depth, worker
online/busy) and the provider are injectable so the state machine tests run with a real
DB and no sleeps.

The tick never raises — a provider outage must not kill the scheduler. Failures and cap
breaches route through raise_alert (§8.4), edge-triggered so a persistent condition
alerts once, not every 60 seconds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.config import settings
from app.models import GpuLease, GpuLeaseStatus
from app.modules import alerts
from app.modules.media import provisioner as provisioner_mod
from app.modules.media import queue as queue_mod
from app.modules.media.provisioner import GpuProvisioner, PodState

logger = logging.getLogger("app.media.orchestrator")

# Edge-trigger state for repeating alert conditions (cap breach, provision failure):
# alert on entering the condition, re-arm when it clears. Process-local by design — a
# restart re-alerting once is acceptable; an email every tick is not.
_alerted: set[str] = set()


def _alert_once(kind: str, message: str, **context: object) -> None:
    if kind in _alerted:
        return
    _alerted.add(kind)
    alerts.raise_alert(kind, message, **context)


def _clear_alert(kind: str) -> None:
    _alerted.discard(kind)


def _with_provider(provider: GpuProvisioner | None, fn):
    """Run `fn(provider)`, building one from settings when none was injected — and then
    closing the built one (its httpx pool would otherwise leak sockets across ticks in a
    long-running scheduler). Injected providers belong to the caller and stay open."""
    owns = provider is None
    resolved = provider or provisioner_mod.build_provider()
    try:
        return fn(resolved)
    finally:
        if owns:
            close = getattr(resolved, "close", None)
            if close is not None:
                close()


def _aware(dt: datetime) -> datetime:
    # SQLite hands datetimes back tz-naive; normalize to aware UTC so arithmetic against
    # the aware `now` doesn't raise offset-naive/offset-aware TypeErrors (publish.py pattern).
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _lease_cost_cents(started_at: datetime, until: datetime) -> int:
    minutes = max(0.0, (_aware(until) - _aware(started_at)).total_seconds() / 60)
    return round(minutes * settings.gpu_pod_rate_cents_per_minute)


def month_to_date_gpu_cost_cents(session: Session, now: datetime) -> int:
    """Media-compute spend since the start of the current UTC month: closed leases at
    their recorded cost, plus the active lease accrued at the configured rate."""
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = 0
    # Any lease overlapping this month counts: started in-month, still ACTIVE, or closed
    # in-month after a rollover. A boundary-spanning lease contributes only its
    # current-month portion (clamped recompute at the configured rate); a fully in-month
    # closed lease uses its recorded cost.
    leases = session.exec(
        select(GpuLease).where(
            (GpuLease.started_at >= month_start)
            | (GpuLease.status == GpuLeaseStatus.ACTIVE)
            | (GpuLease.ended_at >= month_start)
        )
    ).all()
    for lease in leases:
        started = _aware(lease.started_at)
        if lease.status != GpuLeaseStatus.ACTIVE and lease.ended_at is not None:
            if started >= month_start:
                total += lease.cost_cents
            else:
                total += _lease_cost_cents(month_start, _aware(lease.ended_at))
        else:
            total += _lease_cost_cents(max(started, month_start), now)
    return total


def run_provisioner_tick(
    session: Session,
    now: datetime,
    *,
    provider: GpuProvisioner | None = None,
    queue_depth: Callable[[], int] | None = None,
    worker_online: Callable[[], bool] | None = None,
    worker_busy: Callable[[], bool] | None = None,
) -> None:
    """One boot/teardown decision. Never raises (scheduler tick)."""
    depth_fn = queue_depth or queue_mod.media_queue_depth
    online_fn = worker_online or queue_mod.media_worker_online
    busy_fn = worker_busy or queue_mod.media_worker_busy
    try:
        _decide(session, now, provider, depth_fn, online_fn, busy_fn)
    except Exception as exc:  # noqa: BLE001 — the loop must survive any provider/broker failure
        _alert_once(
            "gpu_provision_failed",
            f"media GPU provisioner tick failed: {exc}",
            provider=settings.gpu_provider,
        )


def _decide(
    session: Session,
    now: datetime,
    provider: GpuProvisioner | None,
    queue_depth: Callable[[], int],
    worker_online: Callable[[], bool],
    worker_busy: Callable[[], bool],
) -> None:
    lease = session.exec(select(GpuLease).where(GpuLease.status == GpuLeaseStatus.ACTIVE)).first()
    depth = queue_depth()

    if lease is None:
        if depth == 0:
            return  # cold path: nothing pending, nothing rented — don't even build a provider
        if not worker_online():
            _boot(session, now, provider, depth)
        return

    # A lease is active. Work pending or a job in flight ⇒ the pod is earning its keep.
    if depth > 0 or worker_busy():
        if lease.idle_since is not None:
            lease.idle_since = None
            session.add(lease)
            session.commit()
        if depth > 0 and not worker_busy() and not worker_online():
            # Work is pending but no worker answers: either the pod is still booting
            # (workers take minutes to join) or the provider reclaimed it out-of-band
            # (spot loss/crash). The provider is the source of truth — no grace timer.
            if _with_provider(provider, lambda p: p.status()) is PodState.NONE:
                _mark_lost(session, now, lease)
        return

    if lease.idle_since is None:
        lease.idle_since = now
        session.add(lease)
        session.commit()
        return

    if _aware(now) - _aware(lease.idle_since) >= timedelta(
        minutes=settings.gpu_idle_teardown_minutes
    ):
        _teardown(session, now, provider, lease)


def _boot(session: Session, now: datetime, provider: GpuProvisioner | None, depth: int) -> None:
    cap = settings.media_gpu_monthly_cap_cents
    if cap > 0:
        spent = month_to_date_gpu_cost_cents(session, now)
        if spent >= cap:
            # Jobs stay parked on the queue (nothing is lost, the text/blog crank is
            # untouched); the operator decides whether to raise the cap.
            _alert_once(
                "gpu_spend_cap",
                f"media GPU monthly cap reached ({spent}c >= {cap}c) — "
                f"provisioning halted with {depth} media job(s) queued",
                spent_cents=spent,
                cap_cents=cap,
            )
            return
        _clear_alert("gpu_spend_cap")

    pod_id = _with_provider(provider, lambda p: p.ensure_worker())
    session.add(GpuLease(provider=settings.gpu_provider, pod_id=pod_id, started_at=now))
    session.commit()
    _clear_alert("gpu_provision_failed")
    logger.info("media GPU pod %s provisioned (%d job(s) pending)", pod_id, depth)


def _mark_lost(session: Session, now: datetime, lease: GpuLease) -> None:
    """Close out a lease whose pod the provider no longer has. Billing already stopped
    (nothing exists to bill); the next tick reboots for whatever is still queued — that's
    what keeps "no manual action" true through a spot reclaim."""
    lease.status = GpuLeaseStatus.LOST
    lease.ended_at = now
    lease.cost_cents = _lease_cost_cents(lease.started_at, now)
    session.add(lease)
    session.commit()
    alerts.raise_alert(
        "gpu_pod_lost",
        f"media GPU pod {lease.pod_id} disappeared at the provider mid-lease — "
        "lease closed; a replacement boots on the next tick if jobs are still queued",
        pod_id=lease.pod_id,
        provider=lease.provider,
    )


def _teardown(
    session: Session, now: datetime, provider: GpuProvisioner | None, lease: GpuLease
) -> None:
    verified = _with_provider(provider, lambda p: p.teardown(lease.pod_id))
    lease.ended_at = now
    lease.cost_cents = _lease_cost_cents(lease.started_at, now)
    if verified:
        lease.status = GpuLeaseStatus.ENDED
        logger.info(
            "media GPU pod %s torn down after idle (cost %dc)", lease.pod_id, lease.cost_cents
        )
    else:
        # DELETE was accepted but the pod is still visible — billing may be running.
        # Flag it loudly rather than pretending the money stopped.
        lease.status = GpuLeaseStatus.TEARDOWN_UNVERIFIED
        alerts.raise_alert(
            "gpu_teardown_unverified",
            f"media GPU pod {lease.pod_id} teardown could not be verified — "
            "check the provider console; billing may still be running",
            pod_id=lease.pod_id,
            provider=lease.provider,
        )
    session.add(lease)
    session.commit()
