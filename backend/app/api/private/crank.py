"""Crank API (private dashboard, story S4.1.1 — PRD FR-19, G5).

The scheduled crank (S4.1) is the steady-state path: an hourly tick enqueues one `crank` per LIVE
product whose cadence (weekly by default) has elapsed. That leaves no way to *start* a cycle —
after go-live the operator waits up to an hour for the tick and up to a week for the cadence
before seeing the first piece of content, and the only workaround is a Python shell against the
DB. This route is the operator's "run it now" button (G5: run the cycle without touching code).

Three properties matter more than the endpoint itself:

- **It does not skew the cadence.** Manual runs are recorded under `crank_manual`, which the
  cadence query never looks at. See `MANUAL_CRANK_KIND` in `modules/crank/crank.py`.
- **It does not stack.** A crank already queued or running for the product — scheduled or manual —
  refuses with 409 rather than fanning out (and spending tokens) twice.
- **It refuses rather than no-ops.** A crank with nothing to fan out to is an operator mistake
  worth reporting, not a job to silently queue.

Publishing is deliberately left to the scheduler. A manually cranked item still waits for the next
`_publish_tick` and obeys S4.5 pacing; an operator button that published immediately would defeat
the pacing rules and could burst several posts onto real channels at once.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session

from app.db import get_session
from app.models import Channel, ConnectState, LifecycleState, Product
from app.modules.crank.crank import eligible_channels, enqueue_manual_crank, in_flight_crank

router = APIRouter(prefix="/crank", tags=["crank"])

SessionDep = Annotated[Session, Depends(get_session)]


def _ineligibility_reason(channel: Channel) -> str:
    """Why this channel would be skipped by the fan-out, in the operator's words.

    Only called once `eligible_channels` has excluded the channel, so one of these four branches
    always matches — they mirror that query's filters one for one. Keep them in sync.
    """
    if not channel.enabled:
        return "disabled"
    if channel.paused:
        return "paused"
    if not channel.autonomous:
        return "not autonomous (human-assisted channels are posted by hand in v1)"
    if channel.connect_state == ConnectState.FAILED:
        return "connection failed — reconnect it first"
    # Unreachable while this mirrors `eligible_channels`; here so a filter added there without a
    # matching branch degrades to a vague message instead of falling off the end of the function.
    return "not eligible"  # pragma: no cover


@router.post("/{product_id}", status_code=202)
def trigger_crank(
    product_id: int,
    session: SessionDep,
    channel_id: Annotated[
        int | None,
        Query(description="Crank a single channel instead of every eligible one."),
    ] = None,
) -> dict:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    if product.lifecycle_state != LifecycleState.LIVE:
        raise HTTPException(
            status_code=409,
            detail=f"product is {product.lifecycle_state}, not live; "
            "the crank runs only after go-live",
        )

    # Reported up front so the operator gets the job id they are waiting on. The authoritative,
    # race-free check is inside `enqueue_manual_crank` below — this one is for the message.
    in_flight = in_flight_crank(session, product_id)
    if in_flight is not None:
        raise HTTPException(
            status_code=409,
            detail=f"a crank is already {in_flight.status} for this product "
            f"(job {in_flight.id}); wait for it to finish",
        )

    if channel_id is not None:
        channel = session.get(Channel, channel_id)
        if channel is None or channel.product_id != product_id:
            raise HTTPException(status_code=404, detail="channel not found on this product")
        if not eligible_channels(session, product_id, channel_id):
            raise HTTPException(
                status_code=409,
                detail=f"channel {channel.type} is {_ineligibility_reason(channel)}",
            )
    elif not eligible_channels(session, product_id):
        raise HTTPException(
            status_code=409,
            detail="product has no eligible channels to crank (they must be enabled, "
            "autonomous, unpaused, and not in a failed connection state)",
        )

    job = enqueue_manual_crank(session, product_id, channel_id)
    if job is None:  # lost the race against a concurrent click or the scheduler's tick
        raise HTTPException(
            status_code=409,
            detail="a crank was enqueued for this product concurrently; wait for it to finish",
        )
    return {"job_id": job.id, "status": job.status}
