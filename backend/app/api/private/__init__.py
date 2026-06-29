"""Private dashboard/operator API surface (firewalled, no auth in v1).

Routers for products/strategy/setup/qa/crank/metrics are added in later phases.
"""

from fastapi import APIRouter

from app.api.private import channels, products, qa, setup, strategy

router = APIRouter()


@router.get("/health")
def private_health() -> dict[str, str]:
    return {"surface": "private", "status": "ok"}


router.include_router(products.router)
router.include_router(strategy.router)
router.include_router(setup.router)
router.include_router(channels.router)
router.include_router(qa.router)
