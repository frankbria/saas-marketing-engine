"""Public funnel-ingest API surface (internet-facing, rate-limited, CORS).

Funnel endpoints (visit/lead) and the Stripe webhook are added in Phase 2.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def public_health() -> dict[str, str]:
    return {"surface": "public", "status": "ok"}
