"""FastAPI app factory.

Mounts the two API surfaces (private dashboard API, public funnel API) per
TECH_SPEC §1. No auth in v1 — the private surface is firewalled at deploy time.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

import app.modules.strategy.brand  # noqa: F401 — registers the brand_kit job handler
import app.modules.strategy.brief  # noqa: F401 — registers the strategy_brief job handler
from app.api import private, public
from app.config import settings
from app.db import engine, init_db
from app.scheduler import create_scheduler
from app.secrets.vault import install_redaction
from app.worker import reclaim_running_jobs


@asynccontextmanager
async def lifespan(_app: FastAPI):
    install_redaction()  # scrub vault secrets from all logs before anything runs (§9)
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

    # The dashboard calls the private API cross-origin from the browser (different port);
    # allow only its configured origin(s). The surface stays firewalled at deploy time.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(private.router, prefix="/api/private", tags=["private"])
    app.include_router(public.router, prefix="/api/public", tags=["public"])
    return app


app = create_app()
