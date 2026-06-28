"""FastAPI app factory.

Mounts the two API surfaces (private dashboard API, public funnel API) per
TECH_SPEC §1. No auth in v1 — the private surface is firewalled at deploy time.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlmodel import Session

from app.api import private, public
from app.config import settings
from app.db import engine, init_db
from app.scheduler import create_scheduler
from app.worker import reclaim_running_jobs


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    with Session(engine) as session:
        reclaim_running_jobs(session)  # recover jobs orphaned by a previous crash
    scheduler = create_scheduler()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(private.router, prefix="/api/private", tags=["private"])
    app.include_router(public.router, prefix="/api/public", tags=["public"])
    return app


app = create_app()
