"""Crank scheduling + fan-out (story S4.1, TECH_SPEC §8.1).

`enqueue_due_cranks` is the per-product cadence trigger: a plain, deterministic pass (the scheduler
calls it on an interval, `now` injected so it's trivially testable) that enqueues one `crank`
`job_run` per LIVE product whose cadence has elapsed since its last crank. Due-ness is checked
DB-side (is there a recent crank row?) — no Python tz arithmetic on SQLite-returned datetimes.

The `crank` handler fans out: one `generate` child `job_run` per enabled **autonomous**, non-paused
channel × applicable content type. Children carry `channel_id`/`content_type` so the pipeline knows
its cell. Per-cell job_runs give crash isolation + independent retry (§8.3: "a crashed job never
blocks others"). The handler adds the children without committing — the worker commits them
atomically with the crank's DONE status (matches the brand/site/channels handlers).

The `generate` handler (the real generate step, S4.2) lives in `generate.py`; this module only owns
the cadence trigger + fan-out. S4.1 established that the fan-out carries each cell's identity and
spends no tokens.
"""

import threading
from datetime import datetime, timedelta
from enum import StrEnum

from sqlmodel import Session, select

from app.models import (
    Channel,
    ChannelType,
    ConnectState,
    JobRun,
    JobStatus,
    LifecycleState,
    Product,
)
from app.worker import enqueue, handler

WEEKLY_SECONDS = 7 * 24 * 3600

# S4.1.1 (#82): the operator-triggered crank runs the *same* fan-out under a different `kind`.
#
# Why a separate kind rather than a `trigger` column on job_run: `enqueue_due_cranks` decides
# due-ness by asking "is there a recent `crank` row?", so recording a manual run as `crank` would
# suppress the next scheduled crank for a whole cadence window — the operator's demo run would
# cost the product its real cycle. A distinct kind keeps the cadence query blind to manual runs
# with no query change and, more importantly, no schema change: v1 has no Alembic (`init_db` is
# `create_all`), so a new column would simply not exist on an already-deployed database.
MANUAL_CRANK_KIND = "crank_manual"

_IN_FLIGHT = (JobStatus.QUEUED, JobStatus.RUNNING)
_CRANK_KINDS = ("crank", MANUAL_CRANK_KIND)

# Serialises "is a crank already in flight for this product?" against the insert that follows.
# Without it the check and the insert are a TOCTOU pair: the dashboard POST and the scheduler's
# `_crank_tick` run in the same process on different threads (APScheduler BackgroundScheduler +
# uvicorn's threadpool for sync routes), so both can observe an empty queue and both enqueue —
# a doubled fan-out that spends tokens twice and publishes twice.
#
# ponytail: one process-wide lock, not per-product, and held across the commit. The deployed API
# is a single uvicorn process with no `--workers` (infra/deploy/systemd/sme-api.service), so a
# process-local lock is sufficient and the contention is one short transaction per crank. The
# moment the API runs more than one worker this stops being enough — the upgrade path is a unique
# partial index on (product_id, kind) for in-flight rows, which needs the migration story v1
# does not have yet.
_ENQUEUE_LOCK = threading.Lock()


def in_flight_crank(session: Session, product_id: int) -> JobRun | None:
    """The crank already queued or running for this product, of either kind, if any."""
    return session.exec(
        select(JobRun).where(
            JobRun.product_id == product_id,
            JobRun.kind.in_(_CRANK_KINDS),  # type: ignore[attr-defined]
            JobRun.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
        )
    ).first()


def enqueue_manual_crank(
    session: Session, product_id: int, channel_id: int | None = None
) -> JobRun | None:
    """Enqueue an operator-triggered crank, or return None if one is already in flight.

    The check and the insert happen under `_ENQUEUE_LOCK` so two clicks — or a click landing on
    the same tick as the scheduler — cannot both get past the check.
    """
    with _ENQUEUE_LOCK:
        if in_flight_crank(session, product_id) is not None:
            return None
        return enqueue(session, MANUAL_CRANK_KIND, product_id=product_id, channel_id=channel_id)


class ContentType(StrEnum):
    SOCIAL = "social"
    BLOG = "blog"
    VIDEO = "video"  # Phase B
    PODCAST = "podcast"  # Phase B


# Content types each autonomous channel produces (TECH_SPEC §7/§8.2). Phase A: blog + reddit;
# S5.1 (Phase B) adds youtube→video, S5.2 adds podcast→podcast. The remaining human-assisted
# channels (x/instagram) are intentionally absent.
_CHANNEL_CONTENT_TYPES: dict[ChannelType, tuple[ContentType, ...]] = {
    ChannelType.BLOG: (ContentType.BLOG,),
    ChannelType.REDDIT: (ContentType.SOCIAL,),
    ChannelType.YOUTUBE: (ContentType.VIDEO,),
    ChannelType.PODCAST: (ContentType.PODCAST,),
}


def _cadence_seconds(product: Product) -> int:
    # None or a non-positive (mis)configured value → the weekly default. A non-positive cadence
    # would otherwise push the due-cutoff into the future and re-enqueue a crank on every poll.
    # Clamp rather than raise: one bad product must not crash the tick for every other product.
    cadence = product.crank_cadence_seconds
    return cadence if cadence and cadence > 0 else WEEKLY_SECONDS


def enqueue_due_cranks(session: Session, now: datetime) -> list[JobRun]:
    """Enqueue a `crank` for each LIVE product whose cadence has elapsed. Returns the new rows.

    Takes the same lock as the manual trigger (S4.1.1) so a tick landing while an operator is
    clicking cannot interleave with that check and enqueue a second crank for the product.
    """
    with _ENQUEUE_LOCK:
        products = session.exec(
            select(Product).where(Product.lifecycle_state == LifecycleState.LIVE)
        ).all()
        enqueued: list[JobRun] = []
        for product in products:
            cutoff = now - timedelta(seconds=_cadence_seconds(product))
            # Due-ness looks at `crank` only, never MANUAL_CRANK_KIND — an operator's off-cadence
            # run must not cost the product its next scheduled cycle.
            recent_crank = session.exec(
                select(JobRun).where(
                    JobRun.product_id == product.id,
                    JobRun.kind == "crank",
                    JobRun.created_at >= cutoff,
                )
            ).first()
            if recent_crank is None:  # never cranked, or last crank older than the cadence window
                enqueued.append(enqueue(session, "crank", product_id=product.id))
        return enqueued


def eligible_channels(
    session: Session, product_id: int, channel_id: int | None = None
) -> list[Channel]:
    """Channels a crank would fan out to: enabled, autonomous, unpaused, token still good.

    Shared with the manual-crank route (S4.1.1) so the dashboard can refuse a crank that would
    fan out to nothing, and name the channel it was asked about, instead of queueing a job whose
    only outcome is an empty fan-out.
    """
    query = select(Channel).where(
        Channel.product_id == product_id,
        Channel.enabled,
        Channel.autonomous,
        ~Channel.paused,  # per-channel kill switch (S4.6)
        Channel.connect_state != ConnectState.FAILED,  # dead-token channel (S4.8)
    )
    if channel_id is not None:
        query = query.where(Channel.id == channel_id)
    return list(session.exec(query).all())


@handler("crank")
@handler(MANUAL_CRANK_KIND)
def _run_crank(job: JobRun, session: Session) -> int:
    """Fan out one `generate` child per enabled autonomous channel × content type.

    A manual crank (S4.1.1) may carry a `channel_id`, narrowing the fan-out to that one channel
    so an operator can validate a single connection without spending tokens on all of them.
    """
    if job.product_id is None:
        raise LookupError("crank job has no product_id")
    product = session.get(Product, job.product_id)
    if product is None:
        raise LookupError(f"product {job.product_id} not found")

    channels = eligible_channels(session, product.id, job.channel_id)

    for channel in channels:
        for content_type in _CHANNEL_CONTENT_TYPES.get(channel.type, ()):
            # add (not enqueue) — the worker commits these atomically with the crank's DONE status,
            # so a crank that fails mid-fan-out re-runs cleanly without orphaned children.
            session.add(
                JobRun(
                    kind="generate",
                    product_id=product.id,
                    channel_id=channel.id,
                    content_type=content_type.value,
                )
            )
    return 0  # fan-out spends no tokens
