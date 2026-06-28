"""Private dashboard/operator API surface (firewalled, no auth in v1).

Routers for products/strategy/setup/qa/crank/metrics are added in later phases.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def private_health() -> dict[str, str]:
    return {"surface": "private", "status": "ok"}
