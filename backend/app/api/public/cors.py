"""Per-product CORS for the public funnel endpoints (S2.2).

Each product's landing site lives on its own `marketing_domain`; the funnel endpoints
echo the request Origin back only when it matches that product's domain — so one
product's site can never read another's responses.

Runs as a dedicated middleware mounted *outside* the global CORSMiddleware (which
serves the dashboard): the global one rejects any preflight whose Origin isn't a
configured dashboard origin, so funnel preflights must be handled before it. The
product lookup goes through the app's `get_session` dependency (honouring a test's
override). The Stripe webhook is server-to-server and deliberately has no CORS.
"""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from sqlmodel import select

from app.db import get_session
from app.models.product import Product

_FUNNEL_PREFIX = "/api/funnel/"


def allowed_origins(marketing_domain: str | None) -> set[str]:
    """Acceptable Origin header values for a product's marketing_domain.

    Accepts a bare host (`autoauthor.app`) or a full origin (`https://autoauthor.app`).
    """
    if not marketing_domain:
        return set()
    domain = marketing_domain.strip().rstrip("/")
    if "://" in domain:
        return {domain}
    return {f"https://{domain}", f"http://{domain}"}


def _set_headers(response: Response, origin: str) -> None:
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"


def _marketing_domain(app: FastAPI, slug: str) -> str | None:
    # Use the (possibly test-overridden) session dependency so middleware sees the same DB.
    dependency = app.dependency_overrides.get(get_session, get_session)
    gen = dependency()
    try:
        session = next(gen)
        product = session.exec(select(Product).where(Product.slug == slug)).first()
        return product.marketing_domain if product else None
    finally:
        gen.close()


def install_funnel_cors(app: FastAPI) -> None:
    @app.middleware("http")
    async def funnel_cors(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if not path.startswith(_FUNNEL_PREFIX):
            return await call_next(request)

        origin = request.headers.get("origin")
        slug = path[len(_FUNNEL_PREFIX) :].split("/", 1)[0]
        is_allowed = bool(origin) and origin in allowed_origins(
            _marketing_domain(request.app, slug)
        )

        # Answer the browser preflight here so the global CORSMiddleware never rejects it.
        if request.method == "OPTIONS" and "access-control-request-method" in request.headers:
            response = Response(status_code=204)
            if is_allowed:
                _set_headers(response, origin)
            return response

        response = await call_next(request)
        if is_allowed:
            _set_headers(response, origin)
        return response
