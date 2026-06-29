"""Public funnel-ingest API surface (internet-facing, rate-limited, per-product CORS).

Mounted at `/api` so its routes are the AC paths `/api/funnel/{slug}/…` and
`/api/stripe/webhook`. Health stays at `/api/public/health` (route `/public/health`).
"""

from fastapi import APIRouter

from app.api.public import funnel, stripe

router = APIRouter()


@router.get("/public/health")
def public_health() -> dict[str, str]:
    return {"surface": "public", "status": "ok"}


router.include_router(funnel.router)
router.include_router(stripe.router)
