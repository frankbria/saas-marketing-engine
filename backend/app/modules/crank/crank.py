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

The `generate` handler is the **S4.2 seam**: the real generate→critic→guard→publish pipeline lands
in S4.2 (worker.py: "real crank handlers register here in P4"). S4.1 only enforces that the fan-out
carried each cell's identity; it spends no tokens.
"""

from datetime import datetime, timedelta
from enum import StrEnum

from sqlmodel import Session, select

from app.models import Channel, ChannelType, JobRun, LifecycleState, Product
from app.worker import enqueue, handler

WEEKLY_SECONDS = 7 * 24 * 3600


class ContentType(StrEnum):
    SOCIAL = "social"
    BLOG = "blog"
    VIDEO = "video"  # Phase B
    PODCAST = "podcast"  # Phase B


# Content types each autonomous channel produces in Phase A (TECH_SPEC §7/§8.2). video/podcast
# (Phase B) and the human-assisted channels (x/instagram/youtube) are intentionally absent.
_CHANNEL_CONTENT_TYPES: dict[ChannelType, tuple[ContentType, ...]] = {
    ChannelType.BLOG: (ContentType.BLOG,),
    ChannelType.REDDIT: (ContentType.SOCIAL,),
}


def _cadence_seconds(product: Product) -> int:
    return product.crank_cadence_seconds or WEEKLY_SECONDS


def enqueue_due_cranks(session: Session, now: datetime) -> list[JobRun]:
    """Enqueue a `crank` for each LIVE product whose cadence has elapsed. Returns the new rows."""
    products = session.exec(
        select(Product).where(Product.lifecycle_state == LifecycleState.LIVE)
    ).all()
    enqueued: list[JobRun] = []
    for product in products:
        cutoff = now - timedelta(seconds=_cadence_seconds(product))
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


@handler("crank")
def _run_crank(job: JobRun, session: Session) -> int:
    """Fan out one `generate` child per enabled autonomous channel × content type."""
    if job.product_id is None:
        raise LookupError("crank job has no product_id")
    product = session.get(Product, job.product_id)
    if product is None:
        raise LookupError(f"product {job.product_id} not found")

    channels = session.exec(
        select(Channel).where(
            Channel.product_id == product.id,
            Channel.enabled,
            Channel.autonomous,
            ~Channel.paused,  # per-channel kill switch (S4.6)
        )
    ).all()

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


@handler("generate")
def _run_generate(job: JobRun, _session: Session) -> int:
    """S4.2 seam: validate the fanned-out cell identity. The real pipeline lands in S4.2."""
    if job.product_id is None or job.channel_id is None or job.content_type is None:
        raise LookupError(
            f"generate job {job.id} missing product_id/channel_id/content_type "
            "(should be set by the crank fan-out)"
        )
    return 0
